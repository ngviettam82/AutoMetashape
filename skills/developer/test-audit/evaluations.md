# Evaluation Scenarios: Test Quality & Mutation Audit

### Scenario 1: Auditing Tautological Mock Tests
- **Query**: "Review this new test file for junk patterns and mock abuse."
- **Expected behavior**: Identifies tests that only assert mocks were called with arguments rather than verifying computed outputs.
- **Pass/fail criteria**: Pass if all mock-only assertions are flagged; fail if tautological tests are approved.

### Scenario 2: Mutation Verification
- **Query**: "Verify whether our telemetry unit test catches corrupted GPS coordinate inputs."
- **Expected behavior**: Intentionally mutates GPS parser boundaries; confirms test suite reports a failure.
- **Pass/fail criteria**: Pass if mutation is caught; fail if test passes despite broken logic.

### Scenario 3: Execution Speed Audit
- **Query**: "Check test suite execution time for any slow units."
- **Expected behavior**: Runs pytest with `--durations=10`; flags any unit test exceeding 1.0 second.
- **Pass/fail criteria**: Pass if slow tests are isolated; fail if suite runtime exceeds budget without warning.
