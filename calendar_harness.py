"""Host-tool calendar workflow. No OAuth client or remote calls are implemented here."""
import argparse
from datetime import date, datetime
import hashlib
import json

from expense import read_private, write_private


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


def next_action(state):
    event = state['request']['event']
    action = {'pending': 'search', 'create_attempted': 'recover_by_search',
              'created': 'read', 'verified': 'done'}[state['status']]
    return {'action': action, 'calendar_id': state['request']['calendar_id'],
            'event_id': state.get('event_id', event['id']), 'expected_event': event,
            'rules': ['Use the host calendar tools; do not start OAuth or call another LLM.',
                      'Search document marker and nickname/date; paginate before creating.',
                      'Require actual all-day support for date fields; invite only the given attendee.',
                      'Record create-attempt before creating; after uncertain responses search, never blind retry.',
                      'Read the saved event before verify; do not fabricate tool observations.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'next', 'create-attempt', 'created', 'verify'))
    parser.add_argument('--state', required=True)
    parser.add_argument('--request')
    parser.add_argument('--observation', help='Private JSON containing actual calendar read result')
    parser.add_argument('--event-id')
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
            if state['status'] not in ('pending', 'create_attempted') or not args.event_id:
                raise ValueError('검색/생성으로 확인한 일정 ID가 필요합니다.')
            state.update(status='created', event_id=args.event_id)
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
    print(json.dumps(next_action(state) if state['status'] != 'verified' else state['result'], ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError) as error:
        raise SystemExit(str(error)) from None
