"""Minimal offline check; optionally use privately saved real HPD0550 rows."""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from leave_calendar import calendar_event, leave_balance, prepare_request


def check(rows):
    balance = leave_balance({'basicDy': '15', 'addDy': '0.5', 'useDy': '3.25', 'appProcessDy': '1'})
    assert balance == {'granted': '15.5', 'used_or_reserved': '3.25', 'pending': '1', 'remaining': '11.25'}
    class ReadOnlyBase:
        erp = {'userCode': 'sample-user'}

        def post(self, path, params):
            assert path == '/human/common/attendapplication/getApplicationBaseInfo'
            return {'attendCodeList': [{'atNm': '반반차', 'atCd': 'sample', 'linkAtCd': '1010',
                    'useYn': 'Y', 'deleteYn': 'N', 'timeSetFg': 'NA', 'timeInsYn': 'Y'}]}
    try:
        prepare_request(ReadOnlyBase(), {'start': '2030-01-07', 'type': '반반차'})
    except ValueError as error:
        assert '시작·종료' in str(error)
    else:
        raise AssertionError('Ambiguous time accepted')
    for row in rows:
        event = calendar_event(row, nickname='테스트', email='user@example.org', account=['test'])
        assert event['attendees'] == [{'email': 'user@example.org'}]
        assert event['summary'] == '테스트 ' + row['atNm']
        assert row['linkKey'] not in event['description']
        draft_event = calendar_event({**row, 'approState': '5'}, nickname='테스트',
                                     email='user@example.org', account=['test'], allow_draft=True)
        assert draft_event['id'] == event['id']  # Same document, same id, whether drafted or submitted.
        assert draft_event['description'] == event['description'] == 'leave-sync:' + event['id']
        if row['atNm'] == '연차':
            expected = date.fromisoformat(row['endDt']) + timedelta(days=1)
            assert event['end'] == {'date': expected.isoformat()}
        else:
            clock = row['startTm']
            assert event['start']['dateTime'].endswith(f'T{clock[:2]}:{clock[2:]}:00+09:00')
        for changed in ({'approState': '5'}, {'reportCancYn': 'Y'}, {'endDt': '19990101'}):
            try:
                calendar_event({**row, **changed}, nickname='테스트', email='user@example.org', account=['test'])
            except ValueError:
                pass
            else:
                raise AssertionError('Invalid request accepted')
    print(f'{len(rows)} leave rows checked; no calendar writes')


if __name__ == '__main__':
    if len(sys.argv) > 1:
        rows = json.loads(Path(sys.argv[1]).read_text())
    else:
        base = dict(startDt='20300107', endDt='20300118', startTm='0930', endTm='1830',
                    approState='0', reportCancYn='N', linkKey='sample-document-key')
        rows = [{**base, 'atNm': '연차'},
                {**base, 'atNm': '오후반차', 'endDt': '20300107', 'startTm': '1430'}]
    check(rows)

    # Reconcile: dates decide, titles are advisory, cancelled leave is out of scope.
    from leave_calendar import reconcile
    history = [
        dict(atNm='연차', ycUseCnt=1.0, approState='1', reportCancYn='N', startDt='20300127', endDt='20300127'),
        dict(atNm='오후반차', ycUseCnt=0.5, approState='1', reportCancYn='N', startDt='20300330', endDt='20300330'),
        dict(atNm='연차', ycUseCnt=3.0, approState='1', reportCancYn='N', startDt='20301006', endDt='20301008'),
        dict(atNm='연차', ycUseCnt=1.0, approState='1', reportCancYn='Y', startDt='20300505', endDt='20300505'),
    ]
    observed = [
        {'title': '보람 오후 반차', 'start': '2030-03-31'},          # one day off
        {'title': '보람 휴가(10/2-10/9)', 'start': '2030-10-02', 'end': '2030-10-09'},  # overlaps
        {'title': '보람 대체휴무', 'start': '2030-04-09'},           # calendar only
        {'title': '보람 대체휴무', 'start': '2030-04-09'},           # duplicate
    ]
    result = reconcile(history, observed)
    assert result['groupware_count'] == 3, '취소 건은 제외한다'
    assert [m['groupware']['start'] for m in result['matched']] == ['20301006']
    assert [(n['gap_days'], n['calendar']['start']) for n in result['near_miss']] == [(1, '2030-03-31')]
    assert [g['start'] for g in result['groupware_only']] == ['20300127']
    assert [c['start'] for c in result['calendar_only']] == ['2030-04-09', '2030-04-09']
    assert len(result['calendar_duplicates']) == 1
    try:
        reconcile(history, [{'title': '보람 연차'}])
    except ValueError:
        pass
    else:
        raise AssertionError('start 없는 항목은 거부해야 한다')
    # A wider calendar span is normal: it covers weekends and holidays the leave does not.
    wide = [dict(atNm='연차', ycUseCnt=3.0, approState='1', reportCancYn='N',
                 startDt='20301006', endDt='20301008')]
    away = [{'title': '보람 휴가', 'start': '2030-10-02', 'end': '2030-10-09'}]
    holidays = {date(2030, 10, 3): '개천절', date(2030, 10, 9): '한글날'}
    bare = reconcile(wide, away)
    assert [u['date'] for u in bare['unaccounted_workdays']] == ['2030-10-02', '2030-10-03', '2030-10-04', '2030-10-09']
    aware = reconcile(wide, away, holidays)
    assert [u['date'] for u in aware['unaccounted_workdays']] == ['2030-10-02', '2030-10-04'], '휴일과 주말은 빠져야 한다'
    assert aware['matched'], '기간이 겹치면 여전히 일치로 본다'
    dup = reconcile(history, observed, {})
    assert '2030-03-31' not in [u['date'] for u in dup['unaccounted_workdays']], '하루 어긋남은 중복 보고하지 않는다'
    print('Reconcile passed: overlap matches, one-day gaps flagged, cancelled excluded, duplicates reported')
    print('Holiday filter passed: an eight-day away span narrows to the single unexplained workday')
