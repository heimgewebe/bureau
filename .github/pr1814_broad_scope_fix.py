from pathlib import Path

path = Path("src/bureau/broad_scope_dispatcher.py")
text = path.read_text(encoding="utf-8")
old = '''        *,
        projection_resource: str | None = None,
    ) -> list[str]:
        reasons = super().reasons(
            task,
            capabilities,
            runs,
            reservations,
            overlays,
            projection_resource=projection_resource,
        )
'''
new = '''        *,
        projection_resource: str | None = None,
        initiative_registry: legacy.Registry | None = None,
    ) -> list[str]:
        reasons = super().reasons(
            task,
            capabilities,
            runs,
            reservations,
            overlays,
            projection_resource=projection_resource,
            initiative_registry=initiative_registry,
        )
'''
count = text.count(old)
if count != 1:
    raise SystemExit(f"broad-scope reasons forwarding anchor expected once, found {count}")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
