---
name: test-audit
description: "Audits unit and integration test suites against junk patterns, enforces mutation resistance, and guarantees authoring quality gates across pytest and CTest. Use when authoring, auditing, or refactoring unit tests."
audience: developer
status: stable
owners:
  - core-engineering
---

# Test Quality & Mutation Audit

Engineering standard governing test authoring, mutation resilience, and junk test pattern elimination across unit and integration test suites.

---

## When To Use

Use this skill when:
- Writing new unit tests for subsystems in `source/extensions/*/test/`.
- Auditing existing test suites for flaky, tautological, or mock-heavy tests.
- Verifying code coverage and mutation resistance before merging pull requests.

---

## Workflow

1. **4-Question Gate**:
   Before committing a test, verify:
   - Does it test real production behavior (not mock tautologies)?
   - Does it fail when the underlying implementation is intentionally broken?
   - Is it deterministic and free of race conditions?
   - Does it execute in under 1 second per test unit?
2. **Execution**:
   Run the test runner:
   ```bash
   python -m pytest source/extensions/gaussian.splat/test
   ```

---

## Validation

- Confirm 100% test pass rate with zero flaky tests.
- Run mutation verification (flip relational operators in source; verify test fails).

---

## Maintenance

- Keep `pyproject.toml` `testpaths` updated with all subsystem `test/` directories.
- Purge any test relying on sleep timers.

---

## References

- [pytest Documentation](https://docs.pytest.org/)
- [Mutation Testing Principles](https://en.wikipedia.org/wiki/Mutation_testing)
