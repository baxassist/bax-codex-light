import jsonschema
import pytest
from test_bridge import bridge


@pytest.mark.parametrize("is_other", [False, True])
async def test_native_choice_respects_other_flag_and_0160_wire_schema(
    tmp_path, native_approval_schemas, is_other
):
    b = bridge(tmp_path)
    params = {
        "threadId": "current",
        "turnId": "turn",
        "itemId": "question",
        "isBlocking": True,
        "questions": [
            {
                "id": "next",
                "header": "Дальше",
                "question": "Как продолжить?",
                "isOther": is_other,
                "options": [{"label": "Исправить", "description": "Устранить ошибку подключения"}],
            }
        ],
    }
    schemas = native_approval_schemas["user_input"]
    jsonschema.validate(params, schemas["Params"])
    await b.on_event({"id": 42, "method": "item/tool/requestUserInput", "params": params})
    qid = next(iter(b.questions))
    card = b.relay.sent[-1]
    assert card["kind"] == "choice" and card["options"] == ["Исправить"]
    assert card["allow_custom_answer"] is is_other
    option = "Сначала проверить журнал"
    if not is_other:
        with pytest.raises(ValueError, match="предложенных вариантов"):
            await b.answer({"question_id": qid, "verdict": "choice", "option": option})
        assert qid in b.questions and not b.app.responses
        option = "Исправить"
    await b.answer({"question_id": qid, "verdict": "choice", "option": option})
    request_id, response = b.app.responses[-1]
    assert request_id == 42 and response == {"answers": {"next": {"answers": [option]}}}
    jsonschema.validate(response, schemas["Response"])


async def test_native_question_without_options_accepts_text(tmp_path):
    b = bridge(tmp_path)
    await b.on_event(
        {
            "id": 43,
            "method": "item/tool/requestUserInput",
            "params": {
                "threadId": "current",
                "turnId": "turn",
                "itemId": "question",
                "isBlocking": True,
                "questions": [{"id": "name", "header": "Имя", "question": "Как назвать?", "options": None}],
            },
        }
    )
    qid = next(iter(b.questions))
    assert b.relay.sent[-1]["allow_custom_answer"] is True
    await b.answer({"question_id": qid, "verdict": "choice", "option": "Бакс"})
    assert b.app.responses == [(43, {"answers": {"name": {"answers": ["Бакс"]}}})]
