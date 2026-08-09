from pathlib import Path

path = Path('.github/pr1814_review_fix2.py')
text = path.read_text(encoding='utf-8')

old_search = '''"""        fresh_projected_overlays = dict(fresh_overlays)
        for candidate in fresh_task_candidates:
            fresh_projected_overlays[candidate["task_id"]] = candidate["to_state"]

        for candidate in initiative_candidates:"""'''
new_search = '''"""            fresh_projected_overlays = dict(fresh_overlays)
            for candidate in fresh_task_candidates:
                fresh_projected_overlays[candidate["task_id"]] = candidate["to_state"]

            for candidate in initiative_candidates:"""'''

old_replace = '''"""        fresh_projected_overlays = dict(fresh_overlays)
        for candidate in fresh_task_candidates:
            fresh_projected_overlays[candidate["task_id"]] = candidate["to_state"]
        fresh_diagnostics = {
            item["initiative_id"]: item
            for item in _lifecycle_diagnostics_from_overlays(
                operational_registry, registry, fresh_projected_overlays, store
            )
        }

        for candidate in initiative_candidates:"""'''
new_replace = '''"""            fresh_projected_overlays = dict(fresh_overlays)
            for candidate in fresh_task_candidates:
                fresh_projected_overlays[candidate["task_id"]] = candidate["to_state"]
            fresh_diagnostics = {
                item["initiative_id"]: item
                for item in _lifecycle_diagnostics_from_overlays(
                    operational_registry, registry, fresh_projected_overlays, store
                )
            }

            for candidate in initiative_candidates:"""'''

for old, new, label in (
    (old_search, new_search, 'search anchor'),
    (old_replace, new_replace, 'replacement anchor'),
):
    count = text.count(old)
    if count != 1:
        raise SystemExit(f'{label}: expected exactly one match, found {count}')
    text = text.replace(old, new, 1)

path.write_text(text, encoding='utf-8')
