| scenario | status | detail |
|---|---|---|
| discovery | success | 9 steps, 9 model calls, 23114 tokens, nvidia/nemotron-3-super-120b-a12b |
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
| replay_login_by_human | success | {'savings_balance': '5432.10'}; handoff credentials -> resumed, 4 human actions |
| subaccount_discovery | success | {'confirmation_number': 'CNF-4C4F13', 'primary_share_balance': '$5,432.10'} |
| subaccount_replay_success | success | {'confirmation_number': 'CNF-A672D6', 'primary_share_balance': '5432.10'}; handoff risky_action -> approved, 0 human actions |
| subaccount_replay_other_member | success | {'confirmation_number': 'CNF-5F2EDA', 'primary_share_balance': '1240.50'}; handoff risky_action -> approved, 0 human actions |
| subaccount_replay_validation_error | business_outcome | VALIDATION_ERROR |
| subaccount_replay_escalation_aborted | escalated | escalated at s13_click; handoff risky_action -> denied, 0 human actions |
| tenant_lakeshore_without_overlay | success | {'savings_balance': '5432.10'}; variant lakeshore (no overlay), drift [{'step_id': 's04_click', 'strategy': 'css', 'index': 2}, {'step_id': 's05_type', 'strategy': 'css', 'index': 2}, {'step_id': 's06_click', 'strategy': 'css', 'index': 2}, {'step_id': 's07_click', 'strategy': 'css', 'index': 2}, {'step_id': 's08_extract', 'strategy': 'role_name', 'index': 1}] |
| tenant_lakeshore_with_overlay | success | {'savings_balance': '5432.10'}; variant lakeshore, drift none |
