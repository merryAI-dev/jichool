"""Offline host-result replay. Optional argument: a real private event request JSON."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from calendar_harness import validate_request, verify_event
from expense import write_private, read_private


if __name__ == '__main__':
    request = read_private(sys.argv[1]) if len(sys.argv) > 1 else {
        'calendar_id': 'example-calendar', 'event': {'id': 'abc123', 'summary': '테스트 연차',
        'start': {'date': '2030-01-07'}, 'end': {'date': '2030-01-08'},
        'attendees': [{'email': 'user@example.org'}], 'description': 'leave-sync:abc123'}}
    validate_request(request)
    # Simulated tool response: verifies the harness, not a real calendar write.
    observation = {'calendar_id': request['calendar_id'], 'event': {
        **copy.deepcopy(request['event']), 'id': 'host-generated-id',
        'htmlLink': 'https://calendar.google.com/calendar/event?eid=example'}}
    assert verify_event(request, observation)['event_id'] == 'host-generated-id'
    bad = copy.deepcopy(observation)
    bad['event']['start'] = {'dateTime': request['event']['start']['date']+'T00:00:00+09:00'}
    cases = [bad, {**observation, 'calendar_id': 'wrong-calendar'}]
    for change in ({'attendees': []}, {'description': ''}, {'status': 'cancelled'},
                   {'attendees': observation['event']['attendees']+[{'email': 'extra@example.org'}]},
                   {'hangoutLink': 'https://meet.google.com/example'}, {'summary': 'wrong title'}):
        cases.append({**observation, 'event': {**observation['event'], **change}})
    for bad in cases:
        try:
            verify_event(request, bad)
        except ValueError:
            pass
        else:
            raise AssertionError('Incorrect host result accepted')
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        write_private(root/'request.json', request)
        write_private(root/'read.json', observation)
        def run(*args, success=True):
            result = subprocess.run([sys.executable, 'calendar_harness.py', *args,
                                     '--state', str(root/'state.json')], capture_output=True, text=True)
            assert (result.returncode == 0) == success, result.stderr
            return json.loads(result.stdout) if success else None
        assert run('start', '--request', str(root/'request.json'))['action'] == 'search'
        run('verify', '--observation', str(root/'read.json'), success=False)
        assert run('create-attempt')['action'] == 'recover_by_search'
        run('create-attempt', success=False)
        assert run('next')['action'] == 'recover_by_search'
        run('created', '--event-id', 'host-generated-id')
        assert run('verify', '--observation', str(root/'read.json'))['event_id'] == 'host-generated-id'
        run('start', '--request', str(root/'request.json'), success=False)
        assert read_private(root/'state.json')['status'] == 'verified'
    print('Harness replay passed: all-day, identity, attendees, recovery and completion gates; no calendar writes')
