# Synthetic architecture evaluation

Hand-authored synthetic workflow cases, including a protocol test double for fallback. No organizer practice/hidden cases or live LLM evaluated. Escalation is the expected safe outcome for unsupported unknown causes, not a successful diagnosis.

| Scenario | Expected | Actual | Mutations | Pass |
| --- | --- | --- | --- | --- |
| recovered | resolved | resolved | 1 | True |
| failed | escalated | escalated | 1 | True |
| missing | escalated | escalated | 0 | True |
| rejected | escalated | escalated | 0 | True |
| killed | escalated | escalated | 0 | True |
| unknown | escalated | escalated | 0 | True |
| combined | resolved | resolved | 2 | True |
| oom | resolved | resolved | 1 | True |
| restart-loop | resolved | resolved | 1 | True |
| tier2 | resolved | resolved | 1 | True |
| rollback | escalated | escalated | 2 | True |
| three-causes | escalated | escalated | 0 | True |
