"""Read leave balances/history, prepare a request or calendar event; never write remotely."""
import argparse
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import json
from zoneinfo import ZoneInfo

from expense import Client, ExpenseError, read_private, write_private


def leave_balance(info):
    def total(keys):
        return sum((Decimal(str(info.get(k) or 0)) for k in keys.split()), Decimal(0))
    granted = total('basicDy addDy belowBasicDy boBasicDy boRewDy boChangeDy etcDy rewDy changeDy')
    used = total('useDy useRewDy useChangeDy')
    pending = total('appProcessDy')
    return {k: str(v) for k, v in dict(granted=granted, used_or_reserved=used,
                                      pending=pending, remaining=granted-used-pending).items()}


def leave_status(client, year):
    if len(year) != 4 or not year.isdigit():
        raise ValueError('귀속 연도는 YYYY 형식이어야 합니다.')
    annual = client.post('/personal/hpd0550/0hp00002', {'ycYy': year})
    if len(annual) != 1:
        raise ValueError('해당 연도에 단일 연차 부여 내역이 없습니다.')
    period = {'ycYy': year, 'applyStDt': annual[0].get('applystDt') or year+'0101',
              'applyEdDt': annual[0].get('applyedDt') or year+'1231'}
    rows = client.post('/personal/hpd0550/0hp00003', period)
    fields = ('appDt', 'atNm', 'startDt', 'endDt', 'startTm', 'endTm',
              'ycUseCnt', 'appDy', 'approState', 'reportCancYn', 'linkKey')
    return {'period': period, 'balance': leave_balance(annual[0]),
            'history': [{k: row.get(k) for k in fields} for row in rows]}


def prepare_request(client, request):
    start = datetime.strptime(request['start'], '%Y-%m-%d').date()
    end = datetime.strptime(request.get('end', request['start']), '%Y-%m-%d').date()
    if end < start:
        raise ValueError('종료일이 시작일보다 빠릅니다.')
    if not isinstance(request.get('reason', ''), str):
        raise ValueError('신청 사유는 문자열이어야 합니다.')
    base = client.post('/human/common/attendapplication/getApplicationBaseInfo', {})
    codes = [r for r in base['attendCodeList'] if r['atNm'] == request['type']
             and r.get('useYn') == 'Y' and r.get('deleteYn') == 'N']
    if len(codes) != 1 or codes[0].get('linkAtCd') != '1010':
        raise ValueError('지원하는 연차 근태 항목을 정확히 지정하세요.')
    code = codes[0]
    reason_settings = [r for r in base.get('formSettingList', [])
                       if r.get('linkAtCd') == code['linkAtCd'] and r.get('appItem') == 'APP_REASON_DC']
    reason_required = len(reason_settings) != 1 or reason_settings[0].get('essYn') != 'N'
    params = {'empCd': client.erp['userCode'], 'startDate': start.strftime('%Y%m%d'),
              'endDate': end.strftime('%Y%m%d'), 'atCd': code['atCd'],
              'linkAtCd': code['linkAtCd'], 'timeSetFg': code['timeSetFg']}
    if code.get('timeInsYn') == 'Y':
        if not request.get('start_time') or not request.get('end_time'):
            raise ValueError('시간 직접 입력 항목입니다. 시작·종료 시간을 사람에게 확인하세요.')
        first, last = request['start_time'], request['end_time']
    else:
        times = client.post('/human/common/attendapplication/getStartTmEndTmByWorkType', params)
        if times.get('resultCode') not in (None, 0):
            raise ValueError('근무표 시간 조회가 거절되었습니다.')
        first, last = times['startTm'], times['endTm']
        for key, actual in [('start_time', first), ('end_time', last)]:
            if request.get(key) and request[key].replace(':', '') != actual:
                raise ValueError('요청 시간과 근무표 시간이 다릅니다. 사람에게 확인하세요.')
    first, last = first.replace(':', ''), last.replace(':', '')
    for clock in (first, last):
        if len(clock) != 4:
            raise ValueError('시간은 HH:MM 형식이어야 합니다.')
        datetime.strptime(clock, '%H%M')
    if start == end and last <= first:
        raise ValueError('종료 시간이 시작 시간보다 늦어야 합니다.')
    calculated = client.post('/human/common/attendapplication/calculateApplicationDays', {
        **params, 'startTime': first, 'endTime': last,
        'appRmkDc': request.get('reason', ''), 'calculateOption': 'HOLIDAY_EXCLUSION'})
    if not calculated.get('holidayExclusionApplicationList'):
        raise ValueError('휴일 제외 후 신청 가능한 날짜가 없습니다.')
    overlaps = []
    for year in range(start.year, end.year + 1):
        for row in leave_status(client, str(year))['history']:
            if (row['reportCancYn'] == 'N' and row['approState'] in ('0', '1', '2', '5')
                    and row['startDt'] <= params['endDate'] and row['endDt'] >= params['startDate']):
                overlaps.append(row)
    return {'status': 'prepared_not_submitted', 'request': request,
            'resolved': {**params, 'startTime': first, 'endTime': last, 'atNm': code['atNm']},
            'calculation': calculated, 'overlapping_requests': overlaps,
            'questions': ([] if request.get('reason', '').strip() or not reason_required else ['신청 사유를 알려주세요.'])
                         + (['같은 기간의 기존 신청과 중복되는지 확인해주세요.'] if overlaps else [])}


def calendar_event(row, *, nickname, email, account, allow_draft=False):
    if not nickname.strip() or any(c in nickname for c in '\r\n'):
        raise ValueError('별명을 확인하세요.')
    if email.count('@') != 1 or any(c.isspace() for c in email) or not all(email.split('@')):
        raise ValueError('확인된 법인 이메일을 지정하세요.')
    allowed = ('0', '1', '5') if allow_draft else ('0', '1')
    if row.get('approState') not in allowed or row.get('reportCancYn') != 'N':
        raise ValueError('취소되지 않은 결재진행/완료 문서만 등록합니다.')
    if not row.get('linkKey') or not row.get('atNm'):
        raise ValueError('문서 키 또는 근태 항목이 없습니다.')
    start = datetime.strptime(row['startDt'], '%Y%m%d').date()
    end = datetime.strptime(row['endDt'], '%Y%m%d').date()
    if end < start:
        raise ValueError('휴가 기간이 역전되었습니다.')
    if row['atNm'] == '연차':
        times = {'start': {'date': start.isoformat()},
                 'end': {'date': (end + timedelta(days=1)).isoformat()}}
    else:
        def timestamp(day, clock):
            if len(clock) != 4 or not clock.isdigit():
                raise ValueError('실제 시작/종료 시간이 필요합니다. 사람에게 확인하세요.')
            value = datetime.combine(day, datetime.strptime(clock, '%H%M').time())
            return value.replace(tzinfo=ZoneInfo('Asia/Seoul')).isoformat()
        first = timestamp(start, row.get('startTm', ''))
        last = timestamp(end, row.get('endTm', ''))
        if last <= first:
            raise ValueError('휴가 종료 시간이 시작 시간보다 늦어야 합니다.')
        times = {'start': {'dateTime': first, 'timeZone': 'Asia/Seoul'},
                 'end': {'dateTime': last, 'timeZone': 'Asia/Seoul'}}
    identity = json.dumps([account, row['linkKey']], ensure_ascii=False)
    event_id = hashlib.sha256(identity.encode()).hexdigest()
    marker = 'leave-sync:' + event_id
    description = marker  # One line, no status text: shared calendars reject some payloads silently.
    return {'id': event_id, 'summary': nickname.strip() + ' ' + row['atNm'], **times,
            'attendees': [{'email': email}], 'description': description}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--action', choices=('status', 'prepare', 'event', 'draft-event'), default='event')
    parser.add_argument('--year', default=datetime.now(ZoneInfo('Asia/Seoul')).strftime('%Y'))
    parser.add_argument('--request', help='Private JSON: type, start, end, reason, optional start_time/end_time')
    parser.add_argument('--output', help='New private output file for a prepared request')
    parser.add_argument('--link-key')
    parser.add_argument('--nickname')
    parser.add_argument('--email')
    parser.add_argument('--profile', help='Private JSON containing calendar_id')
    parser.add_argument('--journal', help='Verified leave draft journal, only for explicit draft-calendar requests')
    args = parser.parse_args()
    if len(args.year) != 4 or not args.year.isdigit():
        parser.error('--year must be YYYY')
    client = Client()
    if args.action == 'status':
        print(json.dumps(leave_status(client, args.year), ensure_ascii=False, indent=2))
        return
    if args.action == 'prepare':
        if not args.request or not args.output:
            parser.error('prepare requires --request and --output')
        result = prepare_request(client, read_private(args.request))
        write_private(args.output, result)
        print(json.dumps({'status': result['status'], 'resolved': result['resolved'],
                          'questions': result['questions'], 'output': args.output}, ensure_ascii=False))
        return
    if args.action == 'draft-event':
        if not all((args.profile, args.journal, args.nickname, args.email)):
            parser.error('draft-event requires --profile, --journal, --nickname and --email')
        from leave_draft import draft
        journal = read_private(args.journal)
        if journal.get('stage') != 'verified':
            raise ValueError('검증된 임시저장 기록만 캘린더로 변환합니다.')
        draft(client, journal['request'], args.journal)  # Verified stage performs read-only revalidation.
        journal = read_private(args.journal)
        resolved = journal['plan']['resolved']
        row = dict(approState='5', reportCancYn='N', linkKey=journal['link']['linkKey'],
                   atNm=resolved['atNm'], startDt=resolved['startDate'], endDt=resolved['endDate'],
                   startTm=resolved['startTime'], endTm=resolved['endTime'])
        profile = read_private(args.profile)
        if not isinstance(profile.get('calendar_id'), str) or not profile['calendar_id'].strip():
            raise ValueError('calendar_id 설정이 필요합니다.')
        event = calendar_event(row, nickname=args.nickname, email=args.email,
                               account=journal['account'], allow_draft=True)
        print(json.dumps({'calendar_id': profile['calendar_id'], 'event': event}, ensure_ascii=False, indent=2))
        return
    if not all((args.profile, args.link_key, args.nickname, args.email)):
        parser.error('event requires --profile, --link-key, --nickname and --email')
    profile = read_private(args.profile)
    if not isinstance(profile.get('calendar_id'), str) or not profile['calendar_id'].strip():
        parser.error('profile requires calendar_id')
    rows = leave_status(client, args.year)['history']
    matches = [row for row in rows if row.get('linkKey') == args.link_key]
    if len(matches) != 1:
        raise ValueError('단일 휴가 건을 식별하지 못했습니다. 재상신하지 말고 확인하세요.')
    account = [client.uc['groupSeq'], client.erp['companyCode'], client.erp['userCode']]
    event = calendar_event(matches[0], nickname=args.nickname, email=args.email, account=account)
    print(json.dumps({'calendar_id': profile['calendar_id'], 'event': event}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ExpenseError, ValueError, KeyError, TypeError) as error:
        raise SystemExit(str(error)) from None
