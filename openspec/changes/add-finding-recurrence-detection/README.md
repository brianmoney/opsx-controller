# add-finding-recurrence-detection

The implement-review loop cannot detect that it is re-reporting the same defect round after round, because reviewer findings exist only as free text inside `fix_prompt` and controller state retains only severity counts. Changes burn their full round budget circling one unfixed symbol while `no_progress_limit` stays silent, because the implementer is editing files — just not the right behavior.
