"""The evaluation harness (§19).

§19 says of the harness: "build this, it is not optional. Without it there is no
way to tell whether a prompt change helped or hurt." These tests are about the
scoring itself -- if the WER arithmetic or the false-cut rule is wrong, every
release decision built on them is wrong in the same direction and nothing says
so.

The Devanagari normalisation cases are the ones worth reading. Each is a real
way two transcripts of the same sound compare unequal, and each would inflate
Hindi WER unevenly across vendors -- turning a bake-off into a measurement of
transcription conventions.
"""

from __future__ import annotations

import pytest

from uaagro_evals import judge as judge_mod
from uaagro_evals import turn_eval, wer

# --------------------------------------------------------------------------- #
# WER
# --------------------------------------------------------------------------- #


def test_an_exact_match_scores_zero() -> None:
    assert wer.align("डीएपी का रेट क्या है", "डीएपी का रेट क्या है").wer == 0.0


def test_the_three_error_kinds_are_counted_apart() -> None:
    """Deletions mean the recogniser dropped words the farmer said; insertions
    mean it invented some. A single WER number cannot tell those apart, and
    they call for different fixes."""
    result = wer.align("एक दो तीन चार", "एक पाँच तीन चार पाँच")
    assert result.substitutions == 1
    assert result.insertions == 1
    assert result.deletions == 0
    assert result.reference_length == 4
    assert result.wer == pytest.approx(0.5)


def test_a_dropped_word_is_a_deletion() -> None:
    result = wer.align("यूरिया की बोरी का रेट", "यूरिया की बोरी रेट")
    assert result.deletions == 1
    assert result.substitutions == 0


def test_nukta_spelling_is_not_an_error() -> None:
    """``क़`` exists as one code point and as ``क`` + a combining nukta. One
    vendor emits each. Counting the difference as a substitution measures
    Unicode, not recognition.

    Written as code points rather than as typed characters: the two forms are
    visually identical, so a literal in this file proves nothing about which
    one is being tested -- an editor may already have normalised it.
    """
    composed = "\u0958\u0940\u092e\u0924"  # precomposed
    decomposed = "\u0915\u093c\u0940\u092e\u0924"  # the same word, nukta split out

    assert composed != decomposed, "the two forms must differ before folding"
    assert wer.align(composed, decomposed).errors == 0


def test_a_zero_width_joiner_is_not_an_error() -> None:
    assert wer.align("बीज‍", "बीज").errors == 0


def test_devanagari_and_ascii_digits_compare_equal() -> None:
    """A recogniser that writes 50 where the reference says ५० heard the number
    correctly. Which numeral system appears is a formatting decision downstream
    of recognition."""
    assert wer.align("५० किलो", "50 किलो").errors == 0


def test_a_danda_is_not_a_word() -> None:
    """`।` versus `.` is a transcription convention."""
    assert wer.align("ठीक है।", "ठीक है.").errors == 0


def test_two_genuinely_different_words_still_count() -> None:
    """The normalisation must never merge words that sound different -- that
    would make the metric flatter and useless."""
    assert wer.align("यूरिया", "डीएपी").errors == 1


def test_wer_is_not_capped_at_one() -> None:
    """A recogniser that hallucinates a sentence onto a two-word utterance has
    a WER above one, and clamping would hide how badly."""
    result = wer.align("हाँ जी", "हाँ जी मुझे दस बोरी यूरिया चाहिए आज ही")
    assert result.wer > 1.0


def test_a_hallucination_on_silence_is_not_scored_as_perfect() -> None:
    result = wer.align("", "जी बताइए")
    assert result.wer > 0


def test_the_report_names_the_worst_condition() -> None:
    """The headline number is not actionable: a corpus that is mostly clean
    Hindi averages away a recogniser that cannot hear a tractor -- and the
    tractor is what a farmer calls from."""
    corpus = [
        (wer.Utterance("a.wav", "यूरिया की बोरी", "clean_hindi"), "यूरिया की बोरी"),
        (wer.Utterance("b.wav", "यूरिया की बोरी", "clean_hindi"), "यूरिया की बोरी"),
        (
            wer.Utterance("c.wav", "यूरिया की बोरी", "noisy_hindi_tractor"),
            "यूरिया की डोरी",
        ),
    ]
    report = wer.score(corpus)

    worst = report.worst
    assert worst is not None
    assert worst.condition == "noisy_hindi_tractor"
    assert report.by_condition["clean_hindi"].wer == 0.0
    assert 0 < report.overall < worst.wer


def test_a_corpus_reports_the_conditions_it_is_missing() -> None:
    """§19.1 lists fourteen conditions. A corpus missing half of them produces
    numbers that look complete, so the gap is reportable rather than assumed
    away."""
    corpus = [wer.Utterance("a.wav", "x", "clean_hindi")]
    missing = wer.missing_conditions(corpus)

    assert "clean_hindi" not in missing
    assert "marathi" in missing
    assert len(missing) == len(wer.REQUIRED_CONDITIONS) - 1


# --------------------------------------------------------------------------- #
# Turn detection
# --------------------------------------------------------------------------- #


def _fixture(true_end: float, detected: float, *, language: str = "hi-IN") -> turn_eval.TurnFixture:
    return turn_eval.TurnFixture(
        utterance_id="u", language=language, true_end_ms=true_end, detected_end_ms=detected
    )


def test_cutting_in_early_is_a_false_cut() -> None:
    assert turn_eval.judge(_fixture(2_000, 1_500)) is turn_eval.Verdict.FALSE_CUT


def test_a_few_milliseconds_early_is_not_an_interruption() -> None:
    """Measurement noise around the boundary is not a false cut. Treating it as
    one would make the rate a report on the labelling."""
    assert turn_eval.judge(_fixture(2_000, 1_950)) is turn_eval.Verdict.CORRECT


def test_waiting_too_long_is_dead_air() -> None:
    assert turn_eval.judge(_fixture(2_000, 3_500)) is turn_eval.Verdict.DEAD_AIR


def test_a_false_cut_weighs_more_than_dead_air() -> None:
    """§19.2: false cuts are the more damaging error, weighted accordingly.

    A dead-air error costs a second of silence. A false cut talks over a farmer
    who then cannot tell whether they were heard -- and the agent answers half
    a question confidently.
    """
    cutter = turn_eval.evaluate([_fixture(2_000, 1_000)] + [_fixture(2_000, 2_100)] * 9)
    waiter = turn_eval.evaluate([_fixture(2_000, 4_000)] + [_fixture(2_000, 2_100)] * 9)

    assert cutter.by_language["hi-IN"].score > waiter.by_language["hi-IN"].score


def test_the_mean_wait_excludes_false_cuts() -> None:
    """Averaging cuts into the wait would let interruptions improve the
    number, which rewards exactly the wrong behaviour."""
    report = turn_eval.evaluate(
        [_fixture(2_000, 500), _fixture(2_000, 2_400), _fixture(2_000, 2_600)]
    )
    result = report.by_language["hi-IN"]

    assert result.false_cuts == 1
    assert result.mean_wait_ms == pytest.approx(500.0)


def test_the_worst_language_is_named() -> None:
    """§5.1 routes languages to different recognisers with different turn
    strategies, so one mean is the average of things that do not share an
    implementation -- and it hides the tier-C language that is failing."""
    report = turn_eval.evaluate(
        [_fixture(2_000, 2_100, language="hi-IN")] * 10
        + [_fixture(2_000, 900, language="ml-IN")] * 5
    )
    worst = report.worst_language
    assert worst is not None
    assert worst.language == "ml-IN"


def test_more_false_cuts_blocks_the_merge_even_when_the_mean_improves() -> None:
    """§19.5, and the asymmetry that makes the gate worth having.

    The candidate waits less everywhere -- a better composite -- and buys it
    with an interruption. That trade is refused.
    """
    baseline = turn_eval.evaluate([_fixture(2_000, 2_600)] * 20)
    candidate = turn_eval.evaluate([_fixture(2_000, 1_000)] + [_fixture(2_000, 2_050)] * 19)

    assert candidate.by_language["hi-IN"].mean_wait_ms < baseline.by_language["hi-IN"].mean_wait_ms
    result = turn_eval.regression_gate(baseline, candidate)
    assert result.passed is False
    assert any("false cuts" in reason for reason in result.reasons)


def test_trading_dead_air_for_promptness_is_allowed() -> None:
    """Waiting less is what everyone wants, so long as it is not bought with
    interruptions."""
    baseline = turn_eval.evaluate([_fixture(2_000, 3_500)] * 20)
    candidate = turn_eval.evaluate([_fixture(2_000, 2_200)] * 20)

    assert turn_eval.regression_gate(baseline, candidate).passed is True


def test_a_new_language_does_not_block_on_a_missing_baseline() -> None:
    """Adding a language should not fail the gate for having no history."""
    baseline = turn_eval.evaluate([_fixture(2_000, 2_100, language="hi-IN")] * 10)
    candidate = turn_eval.evaluate(
        [_fixture(2_000, 2_100, language="hi-IN")] * 10
        + [_fixture(2_000, 2_200, language="mr-IN")] * 5
    )
    assert turn_eval.regression_gate(baseline, candidate).passed is True


# --------------------------------------------------------------------------- #
# LLM-as-judge
# --------------------------------------------------------------------------- #


def _case(**kwargs: object) -> judge_mod.JudgeCase:
    defaults: dict[str, object] = {
        "case_id": "c1",
        "farmer_said": "डीएपी का रेट क्या है",
        "agent_replied": "जी, डीएपी की बोरी बारह सौ पचास रुपये की है।",
    }
    return judge_mod.JudgeCase(**{**defaults, **kwargs})  # type: ignore[arg-type]


def test_the_judge_prompt_is_versioned() -> None:
    """§19.4 requires it in the repo. Versioned because a prompt that drifts
    silently makes every score before the drift incomparable with every score
    after."""
    assert judge_mod.JUDGE_PROMPT_VERSION >= 1
    assert "आप" in judge_mod.JUDGE_SYSTEM_PROMPT
    assert "invented_fact" in judge_mod.JUDGE_SYSTEM_PROMPT


def test_the_prompt_serialises_tool_results_stably() -> None:
    """A reordered dict is a different prompt for no reason, and would move
    scores between runs of identical code."""
    one = judge_mod.build_prompt(
        _case(tool_results=[{"sku": "DAP-50", "price": 1250}])
    )
    two = judge_mod.build_prompt(
        _case(tool_results=[{"price": 1250, "sku": "DAP-50"}])
    )
    assert one == two


def test_an_unparseable_judgement_scores_one_rather_than_raising() -> None:
    """A judge that returned prose failed. Scoring the case 1 is the safe
    reading -- skipping it would let a model that breaks on hard cases quietly
    shrink the corpus, which improves the average by dropping the hard half.

    It is *not* marked fatal. `fatal` means a verified safety failure -- an
    invented dose, a missed safety path, an ungrounded claim -- and a flaky
    judge must not be able to block every merge by returning prose. A judge
    that fails often enough to matter shows up anyway: the means collapse
    towards 1 and the score gate catches it.
    """
    verdict = judge_mod.parse_verdict(_case(), "I think this reply is fine.", grounded=True)

    assert verdict.register == 1
    assert verdict.brevity == 1
    assert verdict.helpfulness == 1
    assert verdict.fatal is False
    assert "unparseable" in verdict.note


def test_scores_outside_the_scale_are_clamped() -> None:
    verdict = judge_mod.parse_verdict(
        _case(),
        '{"register": 9, "brevity": 0, "helpfulness": 4,'
        ' "invented_fact": false, "missed_safety_path": false, "note": ""}',
        grounded=True,
    )
    assert verdict.register == 5
    assert verdict.brevity == 1


def test_an_ungrounded_reply_is_fatal_whatever_the_model_thought() -> None:
    """Grounding is computed, not judged. §16.3's validator answers it
    deterministically, and §1 N1 makes it the dimension where a wrong answer is
    most expensive."""
    verdict = judge_mod.parse_verdict(
        _case(),
        '{"register": 5, "brevity": 5, "helpfulness": 5,'
        ' "invented_fact": false, "missed_safety_path": false, "note": "great"}',
        grounded=False,
    )
    assert verdict.mean == 5.0
    assert verdict.fatal is True


def test_one_invented_dose_blocks_a_release_that_improved_every_average() -> None:
    """The failure averaging is designed to hide.

    §1 N1: the agent never invents a dose. A release whose means all rose while
    one case invented one has got worse, not better.
    """
    good = judge_mod.Verdict(
        case_id="ok",
        register=4,
        brevity=4,
        helpfulness=4,
        invented_fact=False,
        missed_safety_path=False,
        grounded=True,
    )
    baseline = judge_mod.JudgeReport(verdicts=[good] * 10)

    better_but_invented = judge_mod.Verdict(
        case_id="invented",
        register=5,
        brevity=5,
        helpfulness=5,
        invented_fact=True,
        missed_safety_path=False,
        grounded=True,
    )
    candidate = judge_mod.JudgeReport(verdicts=[*([good] * 9), better_but_invented])

    assert candidate.mean_register > baseline.mean_register
    result = judge_mod.regression_gate(baseline, candidate)
    assert result.passed is False
    assert any("fatal" in reason for reason in result.reasons)


def test_scores_from_different_judge_versions_are_refused() -> None:
    """The judge is a model reading a prompt. Change the prompt and the numbers
    measure different things; comparing them silently is worse than refusing."""
    verdict = judge_mod.Verdict(
        case_id="c",
        register=4,
        brevity=4,
        helpfulness=4,
        invented_fact=False,
        missed_safety_path=False,
        grounded=True,
    )
    baseline = judge_mod.JudgeReport(prompt_version=1, verdicts=[verdict])
    candidate = judge_mod.JudgeReport(prompt_version=2, verdicts=[verdict])

    result = judge_mod.regression_gate(baseline, candidate)
    assert result.passed is False
    assert "re-run the baseline" in result.reasons[0]


def test_a_register_regression_blocks() -> None:
    """§11.3: an agent using तुम or answering in hectares reads as an outsider,
    and that is a quality regression a mean can otherwise absorb."""

    def report(register: int) -> judge_mod.JudgeReport:
        return judge_mod.JudgeReport(
            verdicts=[
                judge_mod.Verdict(
                    case_id=str(i),
                    register=register,
                    brevity=4,
                    helpfulness=4,
                    invented_fact=False,
                    missed_safety_path=False,
                    grounded=True,
                )
                for i in range(10)
            ]
        )

    result = judge_mod.regression_gate(report(5), report(3))
    assert result.passed is False
    assert any("register" in reason for reason in result.reasons)
