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
        assert draft_event['id'] == event['id'] and '임시보관' in draft_event['description']
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
