import argparse
import itertools
import logging
import re
import sqlite3
import time
from collections import abc
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Set, Tuple, Union
from copy import deepcopy
from deepmerge import always_merger
import functools

if TYPE_CHECKING:
    from hassil.expression import Expression, Sentence
    from hassil.intents import SlotList

_LOGGER = logging.getLogger()

LISTS_KEY="lists"
EXP_RULES_KEY="exp_rules"
CURR_EXP_RULE_KEY="_curr_exp_rule"

@dataclass
class LanguageConfig:
    sentences_mtime_ns: int
    sentences_file_size: int
    database_path: Path
    no_correct_patterns: List[re.Pattern] = field(default_factory=list)
    unknown_text: Optional[str] = None


# language -> config
_CONFIG_CACHE: Dict[str, LanguageConfig] = {}


def load_sentences_for_language(
    sentences_dir: Union[str, Path], language: str, database_dir: Union[str, Path]
) -> Optional[LanguageConfig]:
    """Load YAML file for language with sentence templates."""
    sentences_path = Path(sentences_dir) / f"{language}.yaml"
    if not sentences_path.is_file():
        return None

    sentences_stats = sentences_path.stat()
    config = _CONFIG_CACHE.get(language)

    # We will reload if the file modification time or size has changed
    if (
        (config is not None)
        and (sentences_stats.st_mtime_ns == config.sentences_mtime_ns)
        and (sentences_stats.st_size == config.sentences_file_size)
    ):
        # Cache hit
        return config

    try:
        import yaml
    except ImportError as exc:
        raise Exception("pip3 install wyoming-vosk[limited]") from exc

    # Load and verify YAML
    _LOGGER.debug("Loading %s", sentences_path)
    with open(sentences_path, "r", encoding="utf-8") as sentences_file:
        sentences_yaml = yaml.safe_load(sentences_file)
        if not sentences_yaml:
            _LOGGER.warning("Empty YAML file: %s", sentences_path)
            return None

        if not sentences_yaml.get("sentences"):
            _LOGGER.warning("No sentences in %s", sentences_path)
            return None

    database_dir = Path(database_dir)
    database_dir.mkdir(parents=True, exist_ok=True)
    database_path = database_dir / f"{language}.db"

    # Continue loading
    config = LanguageConfig(
        sentences_mtime_ns=sentences_stats.st_mtime_ns,
        sentences_file_size=sentences_stats.st_size,
        database_path=database_path,
    )

    # Load "no correct" patterns
    no_correct_patterns = sentences_yaml.get("no_correct_patterns", [])
    for pattern_text in no_correct_patterns:
        config.no_correct_patterns.append(re.compile(pattern_text))

    # Load text to use for unknown sentences
    config.unknown_text = sentences_yaml.get("unknown_text")

    # Remove existing database
    database_path.unlink(missing_ok=True)

    # Create new database
    db_conn = sqlite3.connect(str(database_path))
    with db_conn:
        db_conn.execute(
            "CREATE TABLE sentences "
            + "(id INTEGER PRIMARY KEY AUTOINCREMENT, input_text TEXT, output_text TEXT);"
        )
        db_conn.execute(
            "CREATE TABLE words " + "(id INTEGER PRIMARY KEY AUTOINCREMENT, word TEXT);"
        )
        db_conn.commit()
        generate_sentences(sentences_yaml, db_conn)

    _CONFIG_CACHE[language] = config

    return config


def generate_sentences(sentences_yaml: Dict[str, Any], db_conn: sqlite3.Connection):
    try:
        import hassil.parse_expression
        import hassil.sample
        from hassil.intents import SlotList, TextChunk, TextSlotList, TextSlotValue
    except ImportError as exc:
        raise Exception("pip3 install wyoming-vosk[limited]") from exc

    start_time = time.monotonic()

    # sentences:
    #   - same text in and out
    #   - in: text in
    #     out: different text out
    #   - in:
    #       - multiple text
    #       - multiple text in
    #     out: different text out
    # lists:
    #   <name>:
    #     - value 1
    #     - value 2
    # expansion_rules:
    #   <name>: sentence template
    templates = sentences_yaml["sentences"]

    # Load slot lists
    slot_lists: Dict[str, SlotList] = {}
    for slot_name, slot_info in sentences_yaml.get("lists", {}).items():
        if isinstance(slot_info, abc.Sequence):
            slot_info = {"values": slot_info}

        slot_values = slot_info.get("values")
        if not slot_values:
            _LOGGER.warning("No values for list %s, skipping", slot_name)
            continue

        slot_list_values: List[TextSlotValue] = []
        for slot_value in slot_values:
            values_in: List[str] = []

            if isinstance(slot_value, str):
                values_in.append(slot_value)
                value_out: str = slot_value
            else:
                # - in: text to say
                #   out: text to output
                value_in = slot_value["in"]
                value_out = slot_value["out"]

                if hassil.intents.is_template(value_in):
                    input_expression = hassil.parse_expression.parse_sentence(value_in)
                    for input_text in hassil.sample.sample_expression(
                        input_expression,
                    ):
                        values_in.append(input_text)
                else:
                    values_in.append(value_in)

            for value_in in values_in:
                slot_list_values.append(
                    TextSlotValue(TextChunk(value_in), value_out=value_out)
                )

        slot_lists[slot_name] = TextSlotList(slot_list_values)

    # Load expansion rules
    expansion_rules: Dict[str, hassil.Sentence] = {}
    for rule_name, rule_text in sentences_yaml.get("expansion_rules", {}).items():
        expansion_rules[rule_name] = hassil.parse_sentence(rule_text)

    # Generate possible sentences
    num_sentences = 0
    words: Set[str] = set()
    for template in templates:
        if isinstance(template, str):
            input_templates: List[str] = [template]
            output_text: Optional[str] = None
        else:
            input_str_or_list = template["in"]
            if isinstance(input_str_or_list, str):
                # One template
                input_templates = [input_str_or_list]
            else:
                # Multiple templates
                input_templates = input_str_or_list

            output_text = template.get("out")

        for input_template in input_templates:
            if hassil.intents.is_template(input_template):
                # Generate possible texts
                input_expression = hassil.parse_expression.parse_sentence(
                    input_template
                )
                for input_text, maybe_output_text, used_substitutions in sample_expression_with_output(
                    input_expression,
                    slot_lists=slot_lists,
                    expansion_rules=expansion_rules,
                ):
                    substituted_output_text = __substitute(output_text or maybe_output_text or input_text, used_substitutions)
                    db_conn.execute(
                        "INSERT INTO sentences (input_text, output_text) VALUES (?, ?)",
                        (input_text, substituted_output_text),
                    )
                    words.update(w.strip() for w in input_text.split())
                    num_sentences += 1
            else:
                # Not a template
                db_conn.execute(
                    "INSERT INTO sentences (input_text, output_text) VALUES (?, ?)",
                    (input_template, output_text or input_template),
                )
                words.update(w.strip() for w in input_template.split())
                num_sentences += 1

        db_conn.commit()

    # Add words
    for word in words:
        db_conn.execute(
            "INSERT INTO words (word) VALUES (?)",
            (word,),
        )

    db_conn.commit()
    end_time = time.monotonic()

    _LOGGER.info(
        "Generated %s sentence(s) with %s unique word(s) in %0.2f second(s)",
        num_sentences,
        len(words),
        end_time - start_time,
    )


def sample_expression_with_output(
    expression: "Expression",
    slot_lists: "Optional[Dict[str, SlotList]]" = None,
    expansion_rules: "Optional[Dict[str, Sentence]]" = None,
    used_substitutions={LISTS_KEY:{}, EXP_RULES_KEY:{}},
) -> Iterable[Tuple[str, Optional[str], dict]]:
    """Sample possible text strings from an expression."""
    from hassil.expression import (
        ListReference,
        RuleReference,
        Sequence,
        SequenceType,
        TextChunk,
    )
    from hassil.intents import TextSlotList
    from hassil.recognize import MissingListError, MissingRuleError
    from hassil.util import normalize_whitespace


    used_substitutions =deepcopy(used_substitutions)
    if isinstance(expression, TextChunk):
        chunk: TextChunk = expression
        if CURR_EXP_RULE_KEY in used_substitutions:
            curr_exp_rule_name = used_substitutions[CURR_EXP_RULE_KEY]
            used_substitutions[EXP_RULES_KEY][curr_exp_rule_name]=chunk.original_text
        yield (chunk.original_text, chunk.original_text, used_substitutions)
    elif isinstance(expression, Sequence):
        seq: Sequence = expression
            
        if seq.type == SequenceType.ALTERNATIVE:
            for item in seq.items:
                yield from sample_expression_with_output(
                    item,
                    slot_lists,
                    expansion_rules,
                    used_substitutions,
                )
        elif seq.type == SequenceType.GROUP:
            seq_sentences = map(
                partial(
                    sample_expression_with_output,
                    slot_lists=slot_lists,
                    expansion_rules=expansion_rules,
                    used_substitutions=used_substitutions,
                ),
                seq.items,
            )
            sentence_texts = itertools.product(*seq_sentences)
            for sentence_words in sentence_texts:
                yield (
                    normalize_whitespace("".join(w[0] for w in sentence_words)),
                    normalize_whitespace(
                        "".join(w[1] for w in sentence_words if w[1] is not None)
                    ),
                    functools.reduce(always_merger.merge, [w[-1] for w in sentence_words])
                )
        else:
            raise ValueError(f"Unexpected sequence type: {seq}")
    elif isinstance(expression, ListReference):
        # {list}
        list_ref: ListReference = expression
        if (not slot_lists) or (list_ref.list_name not in slot_lists):
            raise MissingListError(f"Missing slot list {{{list_ref.list_name}}}")

        slot_list = slot_lists[list_ref.list_name]
        if isinstance(slot_list, TextSlotList):
            text_list: TextSlotList = slot_list

            if not text_list.values:
                # Not necessarily an error, but may be a surprise
                _LOGGER.warning("No values for list: %s", list_ref.list_name)

            for text_value in text_list.values:
                if text_value.value_out:
                    is_first_text = True
                    for input_text, output_text, used_substitutions in sample_expression_with_output(
                        text_value.text_in,
                        slot_lists,
                        expansion_rules,
                        used_substitutions
                    ):
                        if is_first_text:
                            output_text = (
                                str(text_value.value_out)
                                if text_value.value_out is not None
                                else ""
                            )
                            is_first_text = False
                        else:
                            output_text = None

                        used_substitutions[LISTS_KEY][list_ref.list_name]=text_value.value_out
                        yield (input_text, output_text, used_substitutions)
                else:
                    used_substitutions[LISTS_KEY][list_ref.list_name]=text_value.value_out
                    yield from sample_expression_with_output(
                        text_value.text_in,
                        slot_lists,
                        expansion_rules,
                        used_substitutions
                    )
        else:
            raise ValueError(f"Unexpected slot list type: {slot_list}")
    elif isinstance(expression, RuleReference):
        # <rule>
        rule_ref: RuleReference = expression
        if (not expansion_rules) or (rule_ref.rule_name not in expansion_rules):
            raise MissingRuleError(f"Missing expansion rule <{rule_ref.rule_name}>")

        rule_body = expansion_rules[rule_ref.rule_name]
        used_substitutions.update({CURR_EXP_RULE_KEY: rule_ref.rule_name})
        yield from sample_expression_with_output(
            rule_body,
            slot_lists,
            expansion_rules,
            used_substitutions
        )
    else:
        raise ValueError(f"Unexpected expression: {expression}")


def __substitute(out_sentence:str,used_substitutions:dict[str, dict[str,str]])->str:
    """Substitutes templates with text used to generate input text"""
    for list_name, list_item in used_substitutions[LISTS_KEY].items():
        out_sentence = out_sentence.replace("{"+list_name+"}", list_item,1)
    for exp_name, exp_item in used_substitutions[EXP_RULES_KEY].items():
        out_sentence = out_sentence.replace("<"+exp_name+">", exp_item,1)
    return out_sentence

def correct_sentence(
    text: str, config: LanguageConfig, score_cutoff: float = 0.0
) -> str:
    """Correct a sentence using rapidfuzz."""
    if not config.database_path.is_file():
        # Can't correct without a database
        return text

    # Don't correct transcripts that match a "no correct" pattern
    for pattern in config.no_correct_patterns:
        if pattern.match(text):
            return text

    with sqlite3.connect(str(config.database_path)) as db_conn:
        try:
            from rapidfuzz.distance import Levenshtein
            from rapidfuzz.process import extractOne
        except ImportError as exc:
            raise Exception("pip3 install wyoming-vosk[limited]") from exc

        cursor = db_conn.execute("SELECT input_text, output_text from sentences")
        result = extractOne(
            [text],  # critical that this is a list
            cursor,
            processor=lambda s: s[0],
            scorer=Levenshtein.distance,
            scorer_kwargs={"weights": (1, 1, 3)},
        )
        fixed_row, score = result[0], result[1]

        final_text = text
        if (score_cutoff <= 0) or (score <= score_cutoff):
            # Map to output text
            final_text = fixed_row[1]

        _LOGGER.debug(
            "score=%s/%s, original=%s, final=%s", score, score_cutoff, text, final_text
        )

        return final_text


# -----------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sentences-dir", required=True)
    parser.add_argument("--language", required=True)
    parser.add_argument("--database-dir", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG)

    load_sentences_for_language(args.sentences_dir, args.language, args.database_dir)


if __name__ == "__main__":
    main()
