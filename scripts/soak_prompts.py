"""Real-model evals for the free-prose prompts (INSTRUCTIONS.md 6.2, MANUAL: needs Ollama).

Run:  uv run --extra dev python scripts/soak_prompts.py [--runs 3] [--out DIR] [--only cover]

For every rubric (cover letter P4 cover/v4, engagement outline P4C v3) and every invented posting
in tests/fixtures/soak/, it builds the prompt with the app's own builder, calls the app's own
generator against the local Ollama named in the app's settings, and applies deterministic
checks. Each check has a pass-rate floor in tests/fixtures/soak/thresholds.toml (100 percent for
safety items, 80 for style items). A check below its floor is a finding.

Local only. Nothing goes to an online model unless you pass BOTH --hosted and
--confirm-send-online (the fixtures are invented text, but the rule is the app's rule).

TO ADD A PROMPT: write one `Rubric(...)` entry in RUBRICS below (a generator and a list of
`Check`s) and add its key to the choices of --only. Nothing else changes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tomllib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from soak_common import Finding, out_dir, verdict, write_findings  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "soak"
TOOL = "prompts"


@dataclass
class Fixture:
    """The invented person plus everything the checks need to know about the text sent."""

    profile: dict
    postings: list[dict]

    @property
    def proofs(self) -> list[dict[str, str]]:
        return list(self.profile["proof_points"])

    @property
    def variants(self) -> list[tuple[str, str]]:
        return [(k, t) for k, t in self.profile["variants"]]

    @property
    def valid_keys(self) -> set[str]:
        return {p["key"] for p in self.proofs} | {k for k, _ in self.variants}

    @property
    def sources(self) -> list[str]:
        """The person's own words the model was shown (what a claim may be traced to)."""
        out = [self.profile["headline"], self.profile["narrative"]]
        for p in self.proofs:
            out.append(" ".join(x for x in (p["claim"], p["evidence"], p["metric"]) if x))
        out += [t for _, t in self.variants]
        return out

    @property
    def name_sources(self) -> list[str]:
        return [self.profile["resume_text"]]


@dataclass
class Sample:
    """One generation: the validated value (None when the app refused or failed it) and why."""

    posting: dict
    run: int
    value: object | None
    error: str = ""


@dataclass(frozen=True)
class Check:
    id: str
    severity: str  # "safety" or "style"
    rule: str  # the rule broken, one plain phrase
    action: str  # the next step, one plain sentence
    fn: Callable[[Fixture, Sample], bool]  # True = passes; only called when sample.value exists


@dataclass(frozen=True)
class Rubric:
    key: str
    prompt: str  # for example "cover/v3"
    generate: Callable[..., Awaitable[Sample]]
    checks: list[Check] = field(default_factory=list)


# ---------------------------------------------------------------------------- generators
async def _generate(kind: str, client, fx: Fixture, posting: dict, run: int) -> Sample:
    from kestrel.docs import letters

    prompt = letters.build_letter_prompt(
        kind,
        proofs=fx.proofs,
        variants=fx.variants,
        headline=fx.profile["headline"],
        narrative=fx.profile["narrative"],
        evaluation=fx.profile.get("evaluation"),
        research=fx.profile.get("research", ""),
        title=posting["title"],
        company=posting["company"],
        description=posting["description"],
        org_line=posting.get("org_line", ""),
    )
    gen = letters.generate_engagement if kind == letters.OUTLINE else letters.generate_cover
    draft = await gen(
        client,
        user_prompt=prompt,
        valid_keys=fx.valid_keys,
        grounding=fx.sources,
        name_sources=fx.name_sources,
        posting=posting["description"],
    )
    err = ""
    if draft.value is None:
        err = (
            "third person after one repair"
            if getattr(draft, "third_person", False)
            else str(draft.error or "no reply")
        )
    return Sample(posting, run, draft.value, err)


async def generate_cover(client, fx: Fixture, posting: dict, run: int) -> Sample:
    from kestrel.docs import letters

    return await _generate(letters.COVER, client, fx, posting, run)


async def generate_outline(client, fx: Fixture, posting: dict, run: int) -> Sample:
    from kestrel.docs import letters

    return await _generate(letters.OUTLINE, client, fx, posting, run)


# ---------------------------------------------------------------------------- checks
def _body(s: Sample) -> str:
    return getattr(s.value, "body_md", "") or ""


def _first_person(kind: str) -> Callable[[Fixture, Sample], bool]:
    def run(fx: Fixture, s: Sample) -> bool:
        from kestrel.docs import letters

        return not letters.reply_third_person_hits(s.value, kind) and letters.claims_it(_body(s))

    return run


def _greeting(fx: Fixture, s: Sample) -> bool:
    return _body(s).lstrip().startswith("Dear Hiring Team,")


def _words(kind: str) -> Callable[[Fixture, Sample], bool]:
    def run(fx: Fixture, s: Sample) -> bool:
        from kestrel.docs import letters

        low, high = letters.WORD_RANGE[kind]
        return low <= letters.word_count(_body(s)) <= high

    return run


def _claims(fx: Fixture, s: Sample) -> bool:
    from kestrel.docs import letters

    used = getattr(s.value, "claims_used", None) or []
    return bool(used) and not letters.verify_claims(used, fx.valid_keys)


def _details(fx: Fixture, s: Sample) -> bool:
    from kestrel.docs import letters

    return not letters.unverified_details(
        _body(s), fx.sources, fx.name_sources, s.posting["description"]
    )


def _banned(fx: Fixture, s: Sample) -> bool:
    from kestrel.docs import letters

    return not letters.banned_hits(_body(s))


def _plain(fx: Fixture, s: Sample) -> bool:
    from kestrel import writing_check

    return not [v for v in writing_check.check(_body(s)) if v.blocking]


def _exclusions(fx: Fixture, s: Sample) -> bool:
    return len(getattr(s.value, "exclusions", None) or []) >= 3


_CLAIMS = Check(
    "claims_resolve",
    "safety",
    "a claims_used key does not resolve to the person's own text",
    "Open the prompt's claims rule and tighten it so the model cites only the keys it was given.",
    _claims,
)
_DETAILS = Check(
    "no_unverified_details",
    "safety",
    "a number or name is not in the person's own text and is not about the role",
    "Strengthen the prompt's no-invention rule; the model added a detail the person never gave.",
    _details,
)
_BANNED = Check(
    "banned_phrases",
    "style",
    "a banned stock phrase appears",
    "Add the phrase to the prompt's avoid list or the plain-writing rules.",
    _banned,
)
_PLAIN = Check(
    "plain_writing",
    "style",
    "the plain-writing check found a blocking mark",
    "Read the sample, then tighten the plain-writing block in the prompt.",
    _plain,
)

RUBRICS: dict[str, Rubric] = {
    "cover": Rubric(
        "cover",
        "cover/v4",
        generate_cover,
        [
            Check(
                "first_person",
                "safety",
                "the letter is not in the first person",
                "Tighten the first-person rule in cover/v4 (PROMPTS.md P4).",
                _first_person("cover_letter"),
            ),
            Check(
                "greeting",
                "safety",
                'the letter does not open "Dear Hiring Team,"',
                "Make the greeting rule in cover/v4 stricter and put it first.",
                _greeting,
            ),
            Check(
                "word_count",
                "style",
                "the body is outside 250 to 350 words",
                "Restate the length rule in cover/v4 or lower max_tokens.",
                _words("cover_letter"),
            ),
            _CLAIMS,
            _DETAILS,
            _BANNED,
            _PLAIN,
        ],
    ),
    "outline": Rubric(
        "outline",
        "engagement_outline/v3",
        generate_outline,
        [
            Check(
                "first_person",
                "safety",
                "the outline is not in the first person",
                "Tighten the voice rule in engagement_outline/v3 (PROMPTS.md P4C).",
                _first_person("engagement_outline"),
            ),
            Check(
                "exclusions",
                "safety",
                "fewer than three exclusions",
                "Make the exclusions rule in engagement_outline/v3 stricter.",
                _exclusions,
            ),
            Check(
                "word_count",
                "style",
                "the body is outside 600 to 900 words",
                "Restate the length rule in engagement_outline/v3.",
                _words("engagement_outline"),
            ),
            _CLAIMS,
            _DETAILS,
            _BANNED,
            _PLAIN,
        ],
    ),
}


# ---------------------------------------------------------------------------- runner
def load_fixture(folder: Path = FIXTURES) -> Fixture:
    return Fixture(
        json.loads((folder / "profile.json").read_text(encoding="utf-8")),
        json.loads((folder / "postings.json").read_text(encoding="utf-8")),
    )


def load_floors(path: Path = FIXTURES / "thresholds.toml") -> tuple[dict[str, float], dict]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return data["floors"], data.get("overrides", {})


def floor_for(rubric: str, check: Check, floors: dict, overrides: dict) -> float:
    return float(overrides.get(f"{rubric}.{check.id}", floors[check.severity]))


async def run_rubric(rubric: Rubric, client, fx: Fixture, runs: int) -> list[Sample]:
    """Postings in order, `runs` generations each, one at a time (a local model is one GPU)."""
    out: list[Sample] = []
    for posting in fx.postings:
        for n in range(1, runs + 1):
            print(f"  {rubric.key}: {posting['id']} run {n}/{runs}", flush=True)
            try:
                out.append(await rubric.generate(client, fx, posting, n))
            except Exception as exc:  # noqa: BLE001  a soak run records any failure and goes on
                out.append(Sample(posting, n, None, f"{type(exc).__name__}: {exc}"[:200]))
    return out


def score(rubric: Rubric, fx: Fixture, samples: list[Sample], floors: dict, overrides: dict):
    """(per-check rows, findings). A sample with no value fails every check."""
    rows, findings = [], []
    for check in rubric.checks:
        failed: list[Sample] = []
        for s in samples:
            ok = False
            if s.value is not None:
                try:
                    ok = bool(check.fn(fx, s))
                except Exception as exc:  # noqa: BLE001  a broken check must not hide the rest
                    s.error = s.error or f"check {check.id} raised {type(exc).__name__}"
            if not ok:
                failed.append(s)
        total = len(samples)
        rate = 100.0 * (total - len(failed)) / total if total else 0.0
        floor = floor_for(rubric.key, check, floors, overrides)
        rows.append(
            {
                "check": check.id,
                "severity": check.severity,
                "pass_rate": round(rate, 1),
                "floor": floor,
                "failed": len(failed),
                "total": total,
            }
        )
        if rate < floor:
            who = ", ".join(sorted({f"{s.posting['id']}#{s.run}" for s in failed}))[:200]
            why = sorted({s.error for s in failed if s.error})
            findings.append(
                Finding(
                    f"{rubric.prompt} ({rubric.key})",
                    f"{check.rule}: pass rate {rate:.0f} percent, floor {floor:.0f}",
                    check.action,
                    f"failed samples: {who}" + (f"; errors: {'; '.join(why)[:200]}" if why else ""),
                )
            )
    return rows, findings


# ---------------------------------------------------------------------------- environment
def local_client_or_skip(args):
    """(settings, None) when a local Ollama answers, else (None, the plain reason to skip)."""
    from kestrel.config import load_settings
    from kestrel.services.setup_state import probe_ollama

    try:
        settings = load_settings(mode="hosted" if args.hosted else "local")
    except Exception as exc:  # noqa: BLE001  settings problems are a skip with the reason
        return None, f"settings did not load ({type(exc).__name__}: {exc})"
    tier = settings.llm.tiers["tier3"]
    if args.model:
        settings.llm.tiers["tier3"] = tier.model_copy(update={"model": args.model})
        tier = settings.llm.tiers["tier3"]
    backend = settings.llm.backends[tier.backend]
    if backend.external:
        if not (args.hosted and args.confirm_send_online):
            return None, (
                "the writing model (tier3) points at an online service; this tool is local "
                "only unless you pass --hosted and --confirm-send-online"
            )
        return settings, None
    version, tags = probe_ollama(settings, fresh=True)
    if version is None:
        return None, f"Ollama not reachable at {settings.llm.ollama_native_url}; start Ollama"
    if not any(t == tier.model or t.split(":")[0] == tier.model.split(":")[0] for t in tags):
        return None, f"model {tier.model} is not pulled in Ollama; run `ollama pull` for it"
    return settings, None


async def main_async(args) -> int:
    import httpx

    from kestrel.llm.client import LLMClient

    settings, reason = local_client_or_skip(args)
    if settings is None:
        print(f"SOAK SKIP: {TOOL}: {reason}")
        return 0
    fx = load_fixture()
    floors, overrides = load_floors()
    path = out_dir(args.out, TOOL)
    keys = [args.only] if args.only else list(RUBRICS)
    findings: list[Finding] = []
    summary = [f"Model: {settings.llm.tiers['tier3'].model}. Runs per posting: {args.runs}.", ""]
    report: dict = {}
    async with httpx.AsyncClient() as http:
        client = LLMClient(settings, http=http)
        for key in keys:
            rubric = RUBRICS[key]
            print(f"{rubric.prompt}:", flush=True)
            samples = await run_rubric(rubric, client, fx, args.runs)
            rows, found = score(rubric, fx, samples, floors, overrides)
            findings += found
            report[key] = rows
            summary += [
                f"## {rubric.prompt}",
                "",
                "| check | kind | pass | floor |",
                "|---|---|---|---|",
            ]
            summary += [
                f"| {r['check']} | {r['severity']} | {r['pass_rate']}% | {r['floor']}% |"
                for r in rows
            ] + [""]
            (path / f"samples-{key}.json").write_text(
                json.dumps(
                    [
                        {
                            "posting": s.posting["id"],
                            "run": s.run,
                            "error": s.error,
                            "body": _body(s) if s.value is not None else "",
                        }
                        for s in samples
                    ],
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
    (path / "rates.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    write_findings(path, TOOL, findings, summary)
    return verdict(TOOL, findings)


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Real-model evals for the free-prose prompts.")
    p.add_argument("--runs", type=int, default=3, help="generations per posting (default 3)")
    p.add_argument("--out", help="folder for findings.json and findings.md (default: new temp)")
    p.add_argument("--only", choices=sorted(RUBRICS), help="run one rubric")
    p.add_argument("--model", help="writing model to test instead of the app's tier3 model")
    p.add_argument("--hosted", action="store_true", help="allow an online model (needs the next)")
    p.add_argument(
        "--confirm-send-online",
        action="store_true",
        help="confirm the invented fixtures may be sent to the online model",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(main_async(parse(argv)))


if __name__ == "__main__":
    sys.exit(main())
