| Task | State | Tests | Review | Fingerprint | Updated |
|------|-------|-------|--------|-------------|---------|
| T1 | BLOCKED | 16 passed, 0 failed (T1); full suite 35 passed, 2 failed (pre-existing) | FAIL (re-review 2026-11-18: BLOCKER ISS-93 credential in tracked .env.example; ISS-3, ISS-4, ISS-9, ISS-17 resolved; 4 MAJOR + 9 MINOR new) | 8bf29517ac53 | 2026-11-18 |
| T2 | DONE_WITH_ISSUES | 30 passed, 0 failed (T2); 46 passed combined with T1 | FAIL (re-review 2026-11-18: BLOCKER ISS-106 silent-wrong enum mismatch on the live DB; ISS-19 partial; 2 MAJOR + 4 MINOR new) | 45999c73b8c6 | 2026-11-18 |
| T3 | DONE_WITH_ISSUES | 73 passed, 0 failed (T3: 72 unit + 1 regression suite); full suite 138 passed, 2 failed (pre-existing) | PASS (re-review; 1 BLOCKER CODE fixed) | 50fd6d45e981 | 2026-11-17 |
| T4 | DONE_WITH_ISSUES | 98 passed, 0 failed (T4); fixtures hold 100 rows, 98 test ids | PASS (re-review; 1 BLOCKER CODE fixed) | 31c618e5fa3d | 2026-11-17 |
| T5 | DONE_WITH_ISSUES | 79 passed, 0 failed (T5); full suite 312 passed, 0 failed | FAIL on re-review (2nd SELECT-guard BLOCKER, fixed and adversarially verified; no 3rd review run) | 18148918d2d2 | 2026-11-17 |
| T6 | DONE_WITH_ISSUES | 75 passed, 0 failed (T6); full suite 387 passed, 0 failed | PASS (re-review; 1 BLOCKER CODE fixed) | 47eea96f1724 | 2026-11-17 |
| T7 | DONE_WITH_ISSUES | 74 passed, 0 failed (T7); full suite 461 passed, 0 failed | PASS (re-review; 1 BLOCKER CODE fixed) | d7b2a28f62ba | 2026-11-17 |
| T8 | DONE_WITH_ISSUES | 42 passed, 0 failed (T8); full suite 503 passed, 0 failed | PASS (re-review; 1 BLOCKER CODE fixed) | f385f4c19066 | 2026-11-17 |
