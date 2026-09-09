| scenario | status | detail |
|---|---|---|
| discovery | success | 9 steps, 10 model calls, 24434 tokens, nvidia/nemotron-3-super-120b-a12b |
| replay_success | success | {'savings_balance': '5432.10'} |
| replay_other_member | success | {'savings_balance': '1240.50'} |
| replay_not_found | business_outcome | MEMBER_NOT_FOUND |
| replay_permission_denied | business_outcome | PERMISSION_DENIED |
| replay_invalid_input | business_outcome | INVALID_INPUT |
| replay_app_error | failed | APP_ERROR at s07_click |
| replay_maintenance_dialog | success | {'savings_balance': '5432.10'} |
| replay_session_expired | success | {'savings_balance': '5432.10'} |
| replay_handoff_resumed | success | {'savings_balance': '5432.10'}; handoff risky_action -> resumed, 1 human actions |
| replay_handoff_aborted | escalated | escalated at close_it; handoff risky_action -> denied, 0 human actions |
