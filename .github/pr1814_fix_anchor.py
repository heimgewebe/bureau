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

old_validation_search = '''"""            fresh_task_states = {
                task.id: fresh_projected_overlays.get(task.id, task.state)
                for task in operational_registry.tasks.values()
                if task.initiative == initiative_id
            }
            if (
                fresh_task_states != candidate["task_states"]
                or _lifecycle_recommendation(current_state, fresh_task_states)
                != candidate["to_state"]
            ):
                raise legacy.StateError(
                    f"initiative {initiative_id} lifecycle inputs changed during reconcile"
                )"""'''
new_validation_search = '''"""                fresh_task_states = {
                    task.id: fresh_projected_overlays.get(task.id, task.state)
                    for task in operational_registry.tasks.values()
                    if task.initiative == initiative_id
                }
                if (
                    fresh_task_states != candidate["task_states"]
                    or _lifecycle_recommendation(current_state, fresh_task_states)
                    != candidate["to_state"]
                ):
                    raise legacy.StateError(
                        f"initiative {initiative_id} lifecycle inputs changed during reconcile"
                    )"""'''

old_validation_replace = '''"""            fresh_diagnostic = fresh_diagnostics[initiative_id]
            if (
                fresh_diagnostic["task_states"] != candidate["task_states"]
                or fresh_diagnostic["recommended_state"] != candidate["to_state"]
                or fresh_diagnostic["completion_verification"]
                != candidate["completion_verification"]
            ):
                raise legacy.StateError(
                    f"initiative {initiative_id} lifecycle inputs changed during reconcile"
                )"""'''
new_validation_replace = '''"""                fresh_diagnostic = fresh_diagnostics[initiative_id]
                if (
                    fresh_diagnostic["task_states"] != candidate["task_states"]
                    or fresh_diagnostic["recommended_state"] != candidate["to_state"]
                    or fresh_diagnostic["completion_verification"]
                    != candidate["completion_verification"]
                ):
                    raise legacy.StateError(
                        f"initiative {initiative_id} lifecycle inputs changed during reconcile"
                    )"""'''

for old, new, label in (
    (old_search, new_search, 'projection search anchor'),
    (old_replace, new_replace, 'projection replacement anchor'),
    (old_validation_search, new_validation_search, 'validation search anchor'),
    (old_validation_replace, new_validation_replace, 'validation replacement anchor'),
):
    count = text.count(old)
    if count != 1:
        raise SystemExit(f'{label}: expected exactly one match, found {count}')
    text = text.replace(old, new, 1)

path.write_text(text, encoding='utf-8')
