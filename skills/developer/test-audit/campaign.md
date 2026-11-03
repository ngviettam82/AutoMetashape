# Test Audit Campaign Runbook

Runbook for conducting systematic test quality audits across repository test suites.

## Audit Checklist
1. **Behavior Verification**: Every test must assert concrete domain invariants.
2. **Mutation Verification**: Manually invert boolean conditions or modify numeric tolerances to confirm tests fail.
3. **Hermeticity**: Tests must clean up all temporary directories, mock files, and background processes.
4. **Platform Portability**: Tests must run cleanly across both Windows and Linux environments.
