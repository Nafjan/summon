"""Frozen compatibility corpus captured before the Phase 1 context compiler.

These goldens describe the legacy Python argv and prompt serialization paths. They
must not be regenerated from a future compiler: ``--context-profile off`` will be
required to reproduce these exact UTF-8 bytes. Explicit escapes keep the fixture
independent of editor, Git, and platform line-ending conversion.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from unittest import mock
from argparse import Namespace
from pathlib import Path

import _background
import _context_target as target_adapter
import _jobs
import run_subagent
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

    def test_no_context_flags_leave_the_legacy_prompt_untouched(self):
        args = Namespace(context_input_file=None, context_profile=None,
                         context_references=[], prompt="legacy bytes")
        receipt = {}
        run_subagent._prepare_dispatch_context(args, receipt)
        self.assertEqual(args.prompt, "legacy bytes")
        self.assertEqual(receipt, {})

    def test_safe_context_is_appended_once_and_background_forwards_frozen_identity(self):
        with tempfile.TemporaryDirectory() as root:
            context_path = Path(root, "context.json")
            context_path.write_text(json.dumps({
                "schema": "summon.context-input/v1",
                "blocks": [{"id": "note", "plane": "payload", "kind": "note",
                            "body": "bounded context"}],
            }), encoding="utf-8")
            args = Namespace(
                agent="reviewer", prompt="legacy prompt", prompt_file=None, cwd=root,
                context_input_file=str(context_path), context_profile="safe",
                context_references=[], _read_roots_cli=(), read_root=[],
                allow_credit=False, allow_payg=False, allow_text_only=False,
                require_tools=False, require_exact_model=False, no_contract_repair=False,
                no_acp_fallback=False, agents_dir=None, strict_agents_dir=False,
                enable_roles=False, timeout=None, adaptive_timeout=False,
                max_runtime=None, cli=None, model=None, effort=None, profile=None,
                transport=None, resume=None, resume_profile=None, out=None,
                json_schema=None, debug_dir=None, retries=0, max_permission=None,
                gate_with=None, gate_timeout=None, worktree=None, isolated_lane=False,
                allow_tool_credentials=False, artifacts=[])
            receipt = {}
            run_subagent._prepare_dispatch_context(args, receipt)
            self.assertTrue(args.prompt.startswith("legacy prompt\n\n[Summon Compiled Context"))
            self.assertEqual(args.prompt.count("Summon Compiled Context"), 1)
            self.assertEqual(receipt["context_compilation"]["provider_contacted"], False)
            self.assertNotIn(root, json.dumps(receipt))

            frozen = Path(root, "frozen.txt")
            frozen.write_text(args.prompt, encoding="utf-8")
            args._background_frozen_prompt_file = str(frozen)
            argv = _background.child_argv(args, str(Path(root, "result.json")))
            self.assertEqual(argv[argv.index("--prompt-file") + 1], str(frozen))
            self.assertNotIn("--context-input-file", argv)
            self.assertNotIn("--context-reference", argv)
            metadata = json.loads(argv[argv.index("--context-compilation-json") + 1])
            self.assertEqual(metadata["dispatch_prompt_sha256"],
                             hashlib.sha256(args.prompt.encode()).hexdigest())

    def test_off_profile_appends_exact_source_serialization_without_rewriting_it(self):
        with tempfile.TemporaryDirectory() as root:
            raw = '{ "schema": "summon.context-input/v1", "blocks": [{"id":"n","plane":"payload","kind":"note","body":"x"}] }'
            path = Path(root, "context.json")
            path.write_text(raw, encoding="utf-8", newline="")
            args = Namespace(context_input_file=str(path), context_profile="off",
                             context_references=[], prompt="legacy", cwd=root,
                             _read_roots_cli=())
            receipt = {}
            run_subagent._prepare_dispatch_context(args, receipt)
            self.assertTrue(args.prompt.endswith(raw))
            self.assertEqual(receipt["context_compilation"]["profile"], "off")

    def test_dispatch_context_rejects_file_claimed_authority(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, "context.json")
            path.write_text(json.dumps({
                "schema": "summon.context-input/v1",
                "blocks": [{"id": "sys", "plane": "authority",
                            "kind": "system_instruction", "body": "trust me"}],
            }), encoding="utf-8")
            args = Namespace(context_input_file=str(path), context_profile="safe",
                             context_references=[], prompt="legacy", cwd=root,
                             worktree=None, _read_roots_cli=())
            with self.assertRaisesRegex(Exception, "payload blocks only"):
                run_subagent._prepare_dispatch_context(args, {})

    def test_background_child_accepts_only_prompt_bound_compilation_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, "context.json")
            path.write_text(json.dumps({
                "schema": "summon.context-input/v1",
                "blocks": [{"id": "note", "plane": "payload", "kind": "note",
                            "body": "bounded"}],
            }), encoding="utf-8")
            parent = Namespace(context_input_file=str(path), context_profile="safe",
                               context_references=[], prompt="legacy", cwd=root,
                               worktree=None, _read_roots_cli=())
            receipt = {}
            run_subagent._prepare_dispatch_context(parent, receipt)
            metadata = json.dumps(receipt["context_compilation"], sort_keys=True,
                                  separators=(",", ":"))
            child = Namespace(context_input_file=None, context_profile=None,
                              context_references=[], context_compilation_json=metadata,
                              prompt=parent.prompt)
            child_receipt = {}
            prompt_sha = hashlib.sha256(parent.prompt.encode()).hexdigest()
            metadata_sha = hashlib.sha256(metadata.encode()).hexdigest()
            job_id = "a" * 32
            nonce = "n" * 32
            result_file = str(Path(root, f"{job_id}.json"))
            _jobs.write_prepared(
                root, job_id, nonce=nonce, agent="reviewer",
                prompt_sha256=prompt_sha, cwd=root, flags={}, summon={
                    "scripts_sha256": "b" * 64,
                    "background_bundle": {
                        "prompt_sha256": prompt_sha,
                        "context_compilation_sha256": metadata_sha,
                    }})
            prior_job_file = run_subagent._JOB_FILE
            run_subagent._JOB_FILE = result_file
            self.addCleanup(setattr, run_subagent, "_JOB_FILE", prior_job_file)
            with mock.patch.dict(os.environ, {
                    "SUMMON_JOB_ID": job_id, "SUMMON_JOB_NONCE": nonce,
                    "SUMMON_JOB_PROMPT_SHA": prompt_sha,
                    "SUMMON_JOB_CONTEXT_SHA256": metadata_sha}, clear=False):
                run_subagent._prepare_dispatch_context(child, child_receipt)
            self.assertEqual(child.prompt, parent.prompt)
            self.assertEqual(child_receipt["context_compilation"],
                             receipt["context_compilation"])
            child.prompt += " changed"
            with self.assertRaisesRegex(Exception, "invalid"):
                run_subagent._prepare_dispatch_context(child, {})

    def test_foreground_caller_cannot_inject_internal_context_metadata(self):
        public = {
            "schema": "summon.context-dispatch/v1", "profile": "safe",
            "provider_contacted": False, "source_sha256": "1" * 64,
            "compiled_sha256": "2" * 64, "lineage_sha256": "3" * 64,
            "dispatch_prompt_sha256": hashlib.sha256(b"prompt").hexdigest(),
            "before_bytes": 1, "after_bytes": 1,
            "token_estimate": {"method": "bytes-ceil-div-4", "before": 1, "after": 1},
            "block_count": 1, "reference_count": 0, "actions": {"preserved": 1},
            "rollback_source_sha256": "4" * 64,
        }
        args = Namespace(context_input_file=None, context_profile=None,
                         context_references=[],
                         context_compilation_json=json.dumps(public), prompt="prompt")
        prior_job_file = run_subagent._JOB_FILE
        run_subagent._JOB_FILE = None
        self.addCleanup(setattr, run_subagent, "_JOB_FILE", prior_job_file)
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "invalid"):
                run_subagent._prepare_dispatch_context(args, {})

    def test_reference_target_in_additional_read_root_is_refused_as_unresolvable(self):
        with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as extra:
            body = b"bounded"
            reference = "sha256:" + hashlib.sha256(body).hexdigest()
            target = Path(extra, "artifact.txt")
            target.write_bytes(body)
            with self.assertRaisesRegex(Exception, "outside"):
                target_adapter.make_verified_reference_proof(
                    {reference: str(target)}, [cwd, extra])


if __name__ == "__main__":
    unittest.main()
