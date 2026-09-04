from bureau import task_specs


def test_task_specs_exposes_no_public_repository_rebind_writer() -> None:
    assert not hasattr(task_specs, "put_repository_identity_rebind")
