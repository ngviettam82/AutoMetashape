---
name: test-audit
description: "Audits unit and integration test suites against 15 junk patterns, enforces mutation resistance, and guarantees authoring quality gates across pytest and CTest. Use when authoring, modifying, auditing, or refactoring unit and integration tests."
audience: developer
status: stable
owners:
  - "@autometashape-maintainers"
---

# Test Audit & Quality Assurance

This skill enforces rigorous test quality gates, mutation proofing, and authoring discipline across all unit and integration test suites, adhering to the NVIDIA Isaac Lab test engineering standards.

---

## When To Use

Use this skill when:
- Authoring new test modules or test cases for photogrammetry subsystems.
- Reviewing test code for anti-patterns, trivial assertions, or missing edge cases.
- Auditing existing test suites for mutation resistance and flakiness.

Do not use this skill when:
- Writing one-off exploratory scratch scripts in temporary development directories.

---

## Workflow

### 1. Four-Question Authoring Gate
Before adding any test, answer and verify:
1. What behavior is this test defending?
2. Does this test fail when the behavior breaks (mutation proof)?
3. Does this test avoid testing framework mocks or tautologies?
4. Is the test hermetic, deterministic, and free of order-dependent state?

### 2. Elimination of 15 Junk Patterns
- No tautological assertions (`assert True`, `assert x == x`).
- No assertion-free smoke tests.
- No loose exception catches without verifying error messages or types.
- No sleep-based synchronization; use deterministic polling or reactive events.
- No hardcoded absolute file system paths.

### 3. Execution
Run test verification suites using project launchers:
```powershell
python -m pytest tests/
```

---

## Validation

Verify test quality:
1. Run test suite: all tests must pass deterministically.
2. Mutation check: introduce a deliberate bug in the target module; verify the test fails.
3. Verify test coverage and isolation across test suites.

---

## Maintenance

- Update test suites whenever public API contracts change.
- Review and refine test audit rules during quarterly engineering retrospectives.

---

## References

- [campaign.md](campaign.md)
- [evaluations.md](evaluations.md)
- [CONTRIBUTING.md](../../../CONTRIBUTING.md)
