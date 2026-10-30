"""Voice status identity guards; original controller only, no RPC/network/run."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bax_codex_light.project import ProjectController


def controller():
    owner = ProjectController.__new__(ProjectController)
    owner.sessions = {'owned-session': SimpleNamespace(outbox={}, delivery_receipts={})}
    owner.app = SimpleNamespace(
        items=AsyncMock(return_value={'data': [
            {'item': {}, 'turnId': 'unrelated-turn-without-client-id'},
            {'item': {'clientId': 'owned-client-id'}, 'turnId': 'owned-turn'},
        ]}),
        recent_turns=AsyncMock(return_value=[{'id': 'owned-turn', 'status': 'completed'}]),
    )
    owner.send = AsyncMock(return_value=True)
    return owner


@pytest.mark.parametrize('identity', ['missing', None, '', ' ', 7, {}])
async def test_voice_status_without_nonempty_client_id_never_matches_unidentified_history(identity):
    owner = controller()
    frame = {'type': 'voice.status', 'session': 'owned-session', 'rid': 'owned-generation', 'turn_id': ''}
    if identity != 'missing':
        frame['client_id'] = identity
    await owner.on_frame(frame)
    owner.app.items.assert_not_awaited()
    owner.app.recent_turns.assert_not_awaited()
    owner.send.assert_awaited_once_with('voice.status', session='owned-session', rid='owned-generation',
                                      turn_id='', status='unknown')


async def test_voice_status_valid_stable_client_id_binds_only_original_history_match():
    owner = controller()
    await owner.on_frame({'type': 'voice.status', 'session': 'owned-session', 'rid': 'owned-generation',
                          'client_id': 'owned-client-id', 'turn_id': ''})
    owner.app.items.assert_awaited_once_with('owned-session', None, 100)
    owner.app.recent_turns.assert_awaited_once_with('owned-session')
    owner.send.assert_awaited_once_with('voice.status', session='owned-session', rid='owned-generation',
                                      turn_id='owned-turn', status='completed')


async def test_voice_status_old_explicit_turn_remains_compatible_without_client_id():
    owner = controller()
    await owner.on_frame({'type': 'voice.status', 'session': 'owned-session', 'rid': 'old-rid', 'turn_id': 'owned-turn'})
    owner.app.items.assert_not_awaited()
    owner.app.recent_turns.assert_awaited_once_with('owned-session')
    owner.send.assert_awaited_once_with('voice.status', session='owned-session', rid='old-rid',
                                      turn_id='owned-turn', status='completed')
