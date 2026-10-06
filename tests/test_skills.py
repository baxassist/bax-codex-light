from test_project import project_controller


def skill(name, enabled=True):
    return {
        "name": name,
        "description": "Описание",
        "enabled": enabled,
        "path": f"/skills/{name}/SKILL.md",
        "scope": "user",
    }


async def test_installed_skills_are_project_scoped_enabled_and_readonly(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        fake.skills = [skill("z-last"), skill("disabled", False), skill("a-first")]
        await owner.select("a")
        fake.calls.clear()
        await owner.on_frame({"type": "skills.list", "rid": "picker"})
        receipt = owner.relay.frames[-1]
        assert receipt["type"] == "skills" and receipt["rid"] == "picker"
        assert [row["name"] for row in receipt["skills"]] == ["a-first", "z-last"]
        assert all(set(row) == {"name", "description"} for row in receipt["skills"])
        assert [(c["method"], c["params"]) for c in fake.calls] == [
            ("skills/list", {"cwds": [str(tmp_path)], "forceReload": True})
        ]
        assert owner.selected == "a" and owner.sessions["a"].model_selection is None


async def test_skill_scan_failure_is_reported_instead_of_empty_success(tmp_path):
    async with project_controller(tmp_path) as (fake, owner):
        fake.skill_errors = [{"path": "/broken/SKILL.md", "message": "Invalid skill"}]
        await owner.on_frame({"type": "skills.list", "rid": "failure"})
        frame = owner.relay.frames[-1]
        assert frame["type"] == "skills" and frame["rid"] == "failure"
        assert frame["error"] and frame["skills"] == []
