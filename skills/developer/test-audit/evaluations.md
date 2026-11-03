# Evaluations for test-audit

Evaluation scenarios assessing test suite audit quality.

## Scenario 1: Tautological Assertion Detection
- **Query**: Audit test suite for dummy assertions.
- **Expected Behavior**: Identifies and flags any `assert True` or trivial comparisons.
- **Pass Criteria**: Flags all trivial assertions with actionable remediation suggestions.
- **Fail Criteria**: Permits assertion-free or trivial tests to pass audit.

## Scenario 2: Mutation Proofing Gate
- **Query**: Verify whether test catches inverted return values in camera crop calculations.
- **Expected Behavior**: Detects if mutating the crop calculation function causes unit tests to fail.
- **Pass Criteria**: Unit test fails immediately when deliberate calculation defect is introduced.
- **Fail Criteria**: Test passes despite deliberate calculation mutation.

## Scenario 3: Hermetic Cleanup Verification
- **Query**: Audit tests creating temporary directories or mock datasets.
- **Expected Behavior**: Ensures tests use pytest fixtures like `tmp_path` and leave zero residual files.
- **Pass Criteria**: Zero residual test artifacts left in workspace after test execution.
- **Fail Criteria**: Creates uncleaned temporary files or leaves dangling background handles.
