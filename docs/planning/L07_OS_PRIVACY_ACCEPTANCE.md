# L07 OS privacy acceptance

Status: VERIFIED-FOR-3.5 at the bounded Windows ACL-policy preview scope.
Final candidate execution and publication evidence remain under L09/L12.
Use owned synthetic state only. No real workspace, receipt, account, telemetry
or credential store is needed; no additional OS account should be provisioned.

Integrated execution checkpoint, 2026-09-12: the lead independently passed all
15 current OS-privacy cases in 2.338 seconds on Windows, with zero skips, errors
or failures, unchanged shared source and no bytecode. The prior accepted
reopen packet's 16 definitions are now retained exactly by AST. New checks
cover policy temporary-file protection before write and hardening failure with
closed descriptor, removed temporary and unchanged original policy.

The executed packet includes inherited ACLs, creation/reopen, actual junctions,
hardlinks, unverifiable inspection, export routing and toolchain categories.
It retains the existing limitation to OS ACL-policy verification, without a
separate-principal login or encryption/admin protection claim. On 2026-09-12,
the lead verified the tested module and relevant source remained unchanged,
the fixed `browser_security` command retains the module, and Windows CI runs it
in an explicit platform step. Missing required facilities and non-Windows skips
remain unqualified evidence; they cannot substitute for the zero-skip Windows
result. The final candidate evidence remains separately required under L09/L12.

## Existing seams to reuse

`skills/summon/scripts/_workspace_content.py` secures empty files before content
write and verifies root/file privacy on reopen. Its content tests include real
link/hardlink refusal and an injected reparse-inspection failure. Injected refusal
alone does not establish actual Windows path behavior.

`skills/summon/scripts/test_phase1_fleet_approval.py` contains actual Windows
foreign-ACE hardening tests using hidden `icacls` and ACL snapshots. Reuse the
shared privacy primitives and reviewed fixture pattern, not a real approval
store. Symbolic-link fixtures may skip when unavailable; such a skip does not
qualify the Windows reparse gate. An owned junction is an available alternative
for a directory-reparse branch, not evidence for every file-link variant.

## Required owned journeys

1. Create a workspace/content root beneath a synthetic parent with a foreign
   inheritable read ACE. Verify the resulting root, new blob and host-private
   material satisfy the declared owner-scoped ACL policy before private bytes
   are stored. Record only pass/fail categories, never account identifiers.
2. Reopen with the same declared protection. Seed an unexpected readable ACL
   on an owned existing object and verify typed refusal before reading private
   content or mutation; distinguish deliberate initial hardening from reopen.
3. Exercise actual owned directory reparse and hardlinked-file targets at the
   real content/host boundary, preserving unrelated sentinel bytes. Include an
   unverifiable path/protection failure; do not silently downgrade protection.
4. Verify the actual public export/view projects permitted metadata only and
   cannot expose private paths, content, account identifiers or low-entropy
   fingerprints. Test any private-body export separately under its explicit
   scope and declared destination protection; a read bearer is not broader scope.
5. Retain existing creation/reopen and export success controls alongside the
   refusals. Record Windows/Python/tool availability and all skips explicitly;
   missing required facilities remain an evidence gap, not a successful gate.

ACL snapshots establish permission policy through the OS APIs; they do not
prove behavior under a separately logged-in principal. State that limitation.
Document that ACLs are not encryption and do not protect from an administrator
or the trusted OS user. Optional encryption and broader multi-user operation
remain outside this preview gate. The conductor owns fixture integration and
may reuse prior accepted evidence where the same boundary is actually covered.

Independent token-reopen checkpoint: cumulative repaired privacy/content tests
passed 35 cases in 3.84 seconds, with two explicit exclusions and zero skips.
Source and fresh copy remained unchanged. Actual directory-junction reopen and
unverifiable ancestor inspection refuse before private read; direct reopen,
token bytes/ACL and sentinel preservation pass. Incremental reader guard is
accepted for conductor integration after the prior writer packet. The helper's
bounded grant ended on delivery. Complete L07 status awaits integrated evidence
and precise disposition of the documented test limitations.
