"""Frozen compatibility corpus captured before the Phase 1 context compiler.

These goldens describe the legacy Python argv and prompt serialization paths. They
must not be regenerated from a future compiler: ``--context-profile off`` will be
required to reproduce these exact UTF-8 bytes. Explicit escapes keep the fixture
independent of editor, Git, and platform line-ending conversion.
"""

from __future__ import annotations

import hashlib
import unittest

from _builder import AgentInvocation, _concatenated_prompt
from _cli import rewrite_subcommand


class LegacyCLICompatibilityGoldens(unittest.TestCase):
    def test_legacy_flat_argv_is_the_same_object_and_preserves_multiline_prompt(self):
        argv = ["--agent", "reviewer", "--prompt", "one\ntwo & three"]
        rewritten, mode = rewrite_subcommand(argv)
        self.assertIs(rewritten, argv)
        self.assertIsNone(mode)
        self.assertEqual(rewritten[3].encode("utf-8"), b"one\ntwo & three")

    def test_subcommand_rewrite_corpus_is_stable(self):
        corpus = (
            (["dispatch", "--agent", "reviewer"],
             (["--agent", "reviewer"], None)),
            (["run", "--agent", "reviewer"],
             (["--agent", "reviewer"], None)),
            (["models", "--refresh", "--json"],
             (["--list-models", "--refresh-models", "--json"], None)),
            (["usage", "import", "--from", "snapshot.json", "--cache", "cache.json"],
             (["--usage-action", "import", "--usage-from", "snapshot.json",
               "--usage-cache", "cache.json"], None)),
            (["council", "status", "run-1"],
             (["--council-status", "run-1"], None)),
            (["deliberate", "open", "run-1"],
             (["--deliberate-open", "run-1"], None)),
            (["agents", "validate", "--json"],
             (["--validate-agents", "--json"], None)),
            (["unknown", "value"], (["unknown", "value"], None)),
            (["auth", "repair"],
             (["auth", "repair"],
              "error: 'auth repair' needs a backend (for example kimi)")),
        )
        for argv, expected in corpus:
            with self.subTest(argv=argv):
                self.assertEqual(rewrite_subcommand(argv), expected)


class ContextProfileOffCompatibilityGoldens(unittest.TestCase):
    CASES = (
        (
            "System policy: stay local.",
            "Review the diff.",
            "16464b4906eb74a029748a177053b8ac05d51ead2bdab9dc85af2a55d9069385",
        ),
        (
            "",
            "Prompt only",
            "5d814b9507652ee90642584f0b2496439b39eac4a92709b6e94b92426cc35a5f",
        ),
        (
            "Line A\r\nLine B — café",
            "Question:\r\n• preserve bytes",
            "880cc8243752f1490db20f192ab04183eb816239122fe0fe358a05b81e488799",
        ),
    )

    def test_legacy_serialized_prompt_utf8_bytes_are_frozen(self):
        for system_context, prompt, expected_sha256 in self.CASES:
            with self.subTest(expected_sha256=expected_sha256):
                invocation = AgentInvocation(
                    cli="opencode",
                    cwd="C:/synthetic/project",
                    system_context=system_context,
                    prompt=prompt,
                )
                serialized = _concatenated_prompt(invocation).encode("utf-8")
                self.assertEqual(
                    hashlib.sha256(serialized).hexdigest(), expected_sha256)


if __name__ == "__main__":
    unittest.main()
