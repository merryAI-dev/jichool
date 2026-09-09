"""Host-tool calendar workflow. No OAuth client or remote calls are implemented here."""
import argparse
from datetime import date, datetime, timezone
import base64
import hashlib
import json
from urllib.parse import urlencode

from expense import read_private, write_private, write_private_text


def validate_request(request):
    event = request['event']
    if not isinstance(request['calendar_id'], str) or not request['calendar_id'].strip():
        raise ValueError('대상 캘린더가 필요합니다.')
    if not event['summary'].strip() or len(event['attendees']) != 1:
        raise ValueError('제목과 신청자 참석자 한 명이 필요합니다.')
    if not event['attendees'][0].get('email'):
        raise ValueError('법인 이메일이 필요합니다.')
    if 'leave-sync:' + event['id'] not in event['description']:
        raise ValueError('휴가 문서 식별 표식이 없습니다.')
    first, last = event['start'], event['end']
    if 'date' in first and 'date' in last:
        if date.fromisoformat(last['date']) <= date.fromisoformat(first['date']):
            raise ValueError('종일 일정 종료일은 마지막 휴가일 다음 날이어야 합니다.')
    elif 'dateTime' in first and 'dateTime' in last:
        a, b = datetime.fromisoformat(first['dateTime']), datetime.fromisoformat(last['dateTime'])
        if a.tzinfo is None or b.tzinfo is None or b <= a:
            raise ValueError('시간대가 있는 올바른 시작·종료 시간이 필요합니다.')
    else:
        raise ValueError('종일 날짜와 시간 일정을 혼용할 수 없습니다.')


def verify_event(request, observed):
    """Compare normalized read-tool output, never an agent's success assertion."""
    if observed['calendar_id'] != request['calendar_id']:
        raise ValueError('다른 캘린더에 저장됐습니다.')
    expected, actual = request['event'], observed['event']
    if not actual.get('id') or actual.get('status') == 'cancelled':
        raise ValueError('활성 일정 ID를 확인할 수 없습니다.')
    if actual.get('summary') != expected['summary']:
        raise ValueError('일정 제목이 다릅니다.')
    for field in ('start', 'end'):
        a, b = actual[field], expected[field]
        if 'date' in b:
            if a.get('date') != b['date'] or 'dateTime' in a:
                raise ValueError('종일 일정 날짜가 다릅니다. 자정 시간 일정은 종일 일정이 아닙니다.')
        elif ('dateTime' not in a or datetime.fromisoformat(a['dateTime']) != datetime.fromisoformat(b['dateTime'])):
            raise ValueError('휴가 시간이 다릅니다.')
    attendees = actual.get('attendees', [])
    if len(attendees) != 1 or attendees[0].get('email', '').casefold() != expected['attendees'][0]['email'].casefold():
        raise ValueError('참석자 목록이 다릅니다.')
    if expected['description'] not in actual.get('description', ''):
        raise ValueError('문서 식별 표식 또는 임시보관 안내가 없습니다.')
    if actual.get('hangoutLink') or actual.get('conferenceData'):
        raise ValueError('휴가 일정에 불필요한 회의 링크가 생성됐습니다.')
    url = actual.get('htmlLink', '')
    if not url.startswith(('https://www.google.com/calendar/', 'https://calendar.google.com/')):
        raise ValueError('조회한 일정의 Google 캘린더 링크가 필요합니다.')
    return {'event_id': actual['id'], 'url': url}


def ics_escape(value):
    return str(value).replace('\\', '\\\\').replace(';', '\\;').replace(',', '\\,').replace('\n', '\\n')


def fold(line):
    """RFC 5545 caps octets per line; fold on octet boundaries, not characters."""
    raw = line.encode()
    if len(raw) <= 73:
        return line
    parts, start = [], 0
    while start < len(raw):
        end = min(start + (73 if not parts else 72), len(raw))
        while end < len(raw) and (raw[end] & 0xC0) == 0x80:
            end -= 1
        parts.append(raw[start:end].decode())
        start = end
    return ('\r\n ').join(parts)


def build_ics(request, sequence=0):
    """Importable event. UID is the document-derived id, so re-import updates in place."""
    event = request['event']
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//jichool//leave//KO', 'CALSCALE:GREGORIAN',
             'METHOD:PUBLISH', 'BEGIN:VEVENT', 'UID:' + event['id'] + '@jichool.local',
             'SEQUENCE:' + str(sequence),
             'DTSTAMP:' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')]
    for field, prop in (('start', 'DTSTART'), ('end', 'DTEND')):
        value = event[field]
        if 'date' in value:
            lines.append(prop + ';VALUE=DATE:' + value['date'].replace('-', ''))
        else:
            moment = datetime.fromisoformat(value['dateTime']).astimezone(timezone.utc)
            lines.append(prop + ':' + moment.strftime('%Y%m%dT%H%M%SZ'))
    lines.append('SUMMARY:' + ics_escape(event['summary']))
    lines.append('DESCRIPTION:' + ics_escape(event['description']))
    lines.append('ATTENDEE;ROLE=REQ-PARTICIPANT;RSVP=FALSE:mailto:' + event['attendees'][0]['email'])
    lines += ['TRANSP:TRANSPARENT', 'END:VEVENT', 'END:VCALENDAR']
    return '\r\n'.join(fold(line) for line in lines) + '\r\n'


def template_url(request):
    """Prefilled Google form. The user presses save; this creates no event by itself."""
    event = request['event']
    if 'date' in event['start']:
        span = event['start']['date'].replace('-', '') + '/' + event['end']['date'].replace('-', '')
    else:
        span = '/'.join(datetime.fromisoformat(event[f]['dateTime']).astimezone(timezone.utc)
                        .strftime('%Y%m%dT%H%M%SZ') for f in ('start', 'end'))
    # No 'add': a shared calendar silently drops a save that also invites. Invite after saving.
    return 'https://calendar.google.com/calendar/render?' + urlencode({
        'action': 'TEMPLATE', 'text': event['summary'], 'dates': span,
        'details': event['description'], 'src': request['calendar_id'],
        'ctz': 'Asia/Seoul'})


def decode_eid(raw):
    """Google event pages carry ?eid=base64url("<event id> <calendar id>")."""
    padded = raw + '=' * (-len(raw) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode()).decode()
    except (ValueError, UnicodeDecodeError):
        raise ValueError('eid를 해석할 수 없습니다. 저장된 일정 주소의 eid 값을 그대로 전달하세요.') from None
    event_id = decoded.split(' ')[0].strip()
    if not event_id:
        raise ValueError('eid에서 일정 ID를 찾을 수 없습니다.')
    return event_id


def next_action(state):
    event = state['request']['event']
    action = {'pending': 'search', 'create_attempted': 'recover_by_search',
              'created': 'read', 'handoff_pending': 'await_user_import',
              'verified': 'done'}[state['status']]
    return {'action': action, 'calendar_id': state['request']['calendar_id'],
            'event_id': state.get('event_id', event['id']), 'expected_event': event,
            'template_url': template_url(state['request']),
            'rules': ['Use the host calendar tools; do not start OAuth or call another LLM.',
                      'Search document marker and nickname/date; paginate before creating.',
                      'Require actual all-day support for date fields; invite only the given attendee.',
                      'Record create-attempt before creating; after uncertain responses search, never blind retry.',
                      'Read the saved event before verify; do not fabricate tool observations.',
                      'With no host create tool, run handoff; never report an imported event as verified.',
                      'With only a browser tool, open template_url, confirm the prefilled fields, save, '
                      'then pass the saved page eid to created --eid.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'next', 'create-attempt', 'created', 'handoff', 'verify'))
    parser.add_argument('--state', required=True)
    parser.add_argument('--request')
    parser.add_argument('--observation', help='Private JSON containing actual calendar read result')
    parser.add_argument('--event-id')
    parser.add_argument('--eid', help='eid query value from a saved Google event page')
    parser.add_argument('--ics', help='Path to write the importable .ics for the handoff action')
    args = parser.parse_args()
    if args.action == 'start':
        if not args.request:
            parser.error('start requires --request')
        request = read_private(args.request)
        validate_request(request)
        state = {'status': 'pending', 'request': request}
        write_private(args.state, state)  # Refuse to reset an existing run.
    else:
        state = read_private(args.state)
        validate_request(state['request'])
        if args.action == 'create-attempt':
            if state['status'] != 'pending':
                raise ValueError('신규 생성 단계가 아닙니다. 기존 일정부터 재조회하세요.')
            state['status'] = 'create_attempted'
        elif args.action == 'created':
            if args.eid and not args.event_id:
                args.event_id = decode_eid(args.eid)
            if state['status'] not in ('pending', 'create_attempted', 'handoff_pending') or not args.event_id:
                raise ValueError('검색/생성으로 확인한 일정 ID가 필요합니다.')
            state.update(status='created', event_id=args.event_id)
        elif args.action == 'handoff':
            if state['status'] != 'pending':
                raise ValueError('생성 시도가 이미 진행된 상태입니다. 먼저 재조회하세요.')
            if not args.ics:
                raise ValueError('handoff에는 .ics를 저장할 --ics 경로가 필요합니다.')
            write_private_text(args.ics, build_ics(state['request']))
            state.update(status='handoff_pending', handoff={
                'ics': args.ics, 'template_url': template_url(state['request']),
                'note': '사용자가 직접 저장해야 일정이 생깁니다. 이 단계만으로는 완료가 아닙니다.'})
        elif args.action == 'verify':
            if state['status'] != 'created' or not args.observation:
                raise ValueError('먼저 일정 ID를 기록하고 캘린더 도구로 재조회하세요.')
            observed = read_private(args.observation)
            result = verify_event(state['request'], observed)
            if result['event_id'] != state['event_id']:
                raise ValueError('생성/검색한 일정과 재조회한 일정 ID가 다릅니다.')
            state.update(status='verified', result=result,
                         read_result_sha256=hashlib.sha256(json.dumps(observed, sort_keys=True).encode()).hexdigest())
        if args.action != 'next':
            write_private(args.state, state, replace=True)
    if state['status'] == 'verified':
        payload = state['result']
    elif state['status'] == 'handoff_pending':
        payload = {**next_action(state), 'handoff': state['handoff']}
    else:
        payload = next_action(state)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError) as error:
        raise SystemExit(str(error)) from None
