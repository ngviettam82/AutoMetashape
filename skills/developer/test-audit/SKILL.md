---
name: test-audit
description: "Audits unit and integration test suites against 15 junk patterns, enforces mutation resistance, and guarantees authoring quality gates across Python unittest suites. Use when authoring, modifying, auditing, or refactoring unit and integration tests."
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
1. Tautological assertions (`assert True`, `assert x == x`, or `a or mock.called` hiding failures).
2. Assertion-free smoke tests that only check "did not crash".
3. Testing local inline helper duplicates instead of importing real production code.
4. Testing mock return values rather than production logic.
5. Loose exception catches (`except Exception:` or `assertRaises(Exception)`) without verifying error types/messages.
6. Live host side effects (calling real `taskkill`, mutating shared `%TEMP%/*.pid`, or writing dummy files into repo root).
7. Hardcoded LAN IPs, UNC shares, or absolute local filesystem paths (`\\10.5.10.202\...`, `C:\Users\...`).
8. Global monkeypatches (`patch("os.path.isfile", return_value=True)`) that corrupt unrelated code paths.
9. Cross-test global/module state leakage without `try/finally` restoration.
10. Sleep-based synchronization instead of deterministic mocks or polling.
11. Silent `if available:` skips that hide zero executed assertions.
12. Unseeded nondeterminism or clock/timezone-dependent failures.
13. Invalid/degenerate test fixtures (e.g. face-less `.obj`, invalid ZIP bytes, truncated GLB/PDF headers) masking real gates.
14. Stale/dead assertions checking obsolete filenames or unused dictionary keys.
15. Platform-coupled assumptions (`ntpath` vs `posixpath`, `sys.platform == "win32"`) breaking Linux/Windows CI parity (`.github/workflows/test.yml`).

### 3. Execution
Run test verification suites using the canonical unittest runner (matching CI and `build.bat`):
```powershell
.\build.bat
# Or directly:
python -m unittest discover -s tests
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
