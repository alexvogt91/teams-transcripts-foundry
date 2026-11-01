# Local verification

Verified on 1 November 2026 using Python 3.12.13, Ruff 0.14.1, the declared framework pins and Bicep 0.48.1.
The task branch is `feat/standalone-transcripts-20261101` and remains unborn with uncommitted files.

| Check | Result |
|---|---|
| Function runtime and ingestion tests | 114 passed |
| Portable configuration/deployment/secret-handling tests | 26 passed |
| Pinned-framework agent and candidate/promotion tests | 58 passed |
| Python lint and format | Passed for all 33 Python files |
| Offline PowerShell policy tests | Preview/WhatIf, application preservation, transcription cloning, repeated application, assignment failure and ownership checks passed |
| Bicep | Main template and both `.bicepparam` files compiled with no diagnostics using Bicep 0.48.1 |
| Hosted image | Linux/amd64 build passed; network-disabled import/native-interface smoke passed as UID 10001 |
| Function package | Created on Linux/amd64; required entrypoint/modules verified and all seven native libraries checked as x86-64 ELF |
| Public files | Production identifier, personal path and credential-pattern audit passed |

The total is **198 passing Python tests** with no skips.
The Function and hosted-agent suites use separate environments and independent synthetic fixtures.
No Azure calls, real model calls or tenant policy changes are performed by these tests.
The policy tests use local PowerShell functions rather than a live Teams session.

Bicep uses the public capability-host preview API for hosted agents.
The full-text indexing object has an explicit type escape because older Bicep schemas omit the service's `fullTextIndexes` property; the resulting template preserves the required index.
Compilation does not establish Azure region/model quota, tenant feature availability or live resource-provider acceptance.
Older compilers can report an unavailable preview resource type; use the documented Bicep 0.48.1 or later prerequisite.

Ignored local artifacts include the Function ZIP, compiled ARM templates and local tooling under `.local/`.
No transcript corpus or credentials were written into the public files.
The source workspace was read without changes.

Azure provisioning, application admin consent, Teams policy application, image push, Function publication, hosted candidate deployment/promotion, actual identity-denial probes and real-meeting/playground acceptance were not executed.
Follow the root README's staged commands and live acceptance checklist before claiming a successful cloud rollout.
