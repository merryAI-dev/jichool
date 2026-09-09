"""Create a native leave draft, preserving the current user's form and approval line."""
import argparse
from datetime import datetime
import html
import json
from pathlib import Path
import re
import uuid
import urllib.parse
from zoneinfo import ZoneInfo

from expense import Client, ExpenseError, eap_init, native_draft_payload, read_private, write_private
from leave_calendar import prepare_request, leave_status


def remove_embedded_reset(body):
    # The editor can strip <style> while leaving its CSS as visible document text.
    return re.sub(r'<style\b[^>]*>\s*\*\s*\{\s*margin\s*:\s*0\s*;\s*padding\s*:\s*0\s*;?\s*\}\s*</style>',
                  '', body, flags=re.I)


def render_leave(initial, uc, title):
    binding = json.loads(initial['resultMap']['outProcessForm']['outBindData'])
    body = remove_embedded_reset(initial['mainhtml'])
    def fill(template, values):
        def value(match):
            attrs = dict((k, html.unescape(v)) for k, v in re.findall(r'([\w-]+)="([^"]*)"', match[0]))
            key = attrs.get('mapping_key')
            if key not in values or isinstance(values[key], (dict, list)):
                raise ValueError('지원하지 않는 휴가 본문 연결 항목: ' + str(key))
            return '<span>' + html.escape(str(values[key] if values[key] is not None else '')) + '</span>'
        return re.sub(r'<input\b[^>]*>', value, template, flags=re.I)
    tables = list(re.finditer(r'(<table\b[^>]*mapping_key="dbTable1"[^>]*>)(.*?)(</table>)', body, re.S))
    if len(tables) != 1 or '<table' in tables[0][2]:
        raise ValueError('휴가 표 구조가 변경되었습니다.')
    table = tables[0]
    tbody = re.search(r'(<tbody[^>]*>)(.*?)(</tbody>)', table[2], re.S)
    rows = re.findall(r'<tr\b[^>]*>.*?</tr>', tbody[2] if tbody else '', re.S)
    if len(rows) != 2:
        raise ValueError('휴가 반복 행 구조가 변경되었습니다.')
    parts = []
    for group in binding['TABLE']['dbTable1']['group']:
        if len(group['group']) != 1:
            raise ValueError('현재는 사용자별 단일 휴가 기간만 저장합니다.')
        values = {**binding['ITEMS'], **group['items'], **group['group'][0]['items']}
        parts.append(fill(''.join(rows), values))
    inner = table[2][:tbody.start()] + tbody[1] + ''.join(parts) + tbody[3] + table[2][tbody.end():]
    body = fill(body[:table.start()] + table[1] + inner + table[3] + body[table.end():], binding['ITEMS'])
    result = initial['html']
    fields = {'_DF01_': '', '_DF02_': datetime.now(ZoneInfo('Asia/Seoul')).strftime('%Y-%m-%d'),
              '_DF03_': uc['deptName'], '_DF04_': uc['empName'], '_DF09_': '', '_DF10_': title}
    for key, value in fields.items():
        result = result.replace(key, html.escape(str(value)))
    result = result.replace('_DF11_', '<div id="divInterJson">'+body+'</div>')
    if re.search(r'_DF\w+_|<input\b', result, re.I):
        raise ValueError('본문에 미완성 필드가 남았습니다.')
    return result


def draft(client, request, journal_path):
    journal_path = Path(journal_path)
    account = [client.uc['groupSeq'], client.erp['companyCode'], client.erp['userCode']]
    if journal_path.exists():
        journal = read_private(journal_path)
        if journal['account'] != account or journal['request'] != request:
            raise ValueError('다른 사용자 또는 요청의 작업 기록입니다.')
        if journal['stage'] == 'draft_attempted':
            found = client.post('/system/apiUtilEap/GetEnageGroup', {
                'approKey': journal['approKey'], 'linkKey': journal['link']['linkKey']})
            if not found.get('docId') or str(found.get('approState')) != '5':
                raise ValueError('저장 결과를 확인할 수 없습니다. 재전송하지 않습니다.')
            journal['draft'] = {'result': int(found['docId'])}
            journal['stage'] = 'draft_saved'
            write_private(journal_path, journal, replace=True)
        elif journal['stage'].endswith('_attempted'):
            raise ValueError('이전 쓰기 응답이 불명확합니다. 재전송 전에 서버 상태를 확인하세요.')
    else:
        plan = prepare_request(client, request)
        if plan['questions']:
            raise ValueError(' '.join(plan['questions']))
        application = plan['calculation']['holidayExclusionApplicationList']
        if len(application) != 1:
            raise ValueError('단일 연속 기간만 지원합니다.')
        employee = dict(coCd=client.erp['companyCode'], empCd=client.erp['userCode'],
                        deptCd=client.erp['deptCode'], deptNm=client.erp['deptName'], korNm=client.erp['userName'])
        row = {**application[0], **employee, 'empNm': employee['korNm'], 'atNm': plan['resolved']['atNm']}
        validation = client.post('/human/attendapplication/validateNew', {
            'checkRange': 'ALL', 'checkPoint': 'ADD', 'empCdList': [employee['empCd']],
            'newItem': {**row, 'employeeList': [employee]}, 'alreadyAddedItems': []})
        if validation.get('resultCode') != 0 or any(v for k, v in validation.items() if k.endswith('List')):
            raise ValueError('휴가 서버 검증을 통과하지 못했습니다.')
        forms = client.post('/eap/eap096A45', {
            'header': {'pId': '', 'groupSeq': client.uc['groupSeq'], 'empSeq': client.uc['empSeq']},
            'body': {'companyInfo': {'compSeq': client.uc['compSeq']}, 'formDTp': '',
                     'searchFormDTp': 'HP_HPD0110_00011', 'langCode': client.uc['langCode']}})
        forms = [f for f in forms if f['useYn'] in ('1', 'Y') and f['formDTp'] == 'HP_HPD0110_00011']
        if len(forms) != 1:
            raise ValueError('사용할 휴가 양식을 명확히 선택해야 합니다.')
        form = forms[0]
        initial = eap_init(client, form['formId'])
        if initial['resultMap'].get('appdocinfo') or not initial['resultMap']['hidAppDocLine']:
            raise ValueError('신규 양식 또는 결재선을 확인할 수 없습니다.')
        title = client.erp['userName'] + ' ' + plan['resolved']['atNm'] + ' (' + request['start'] + ')'
        journal = {'account': account, 'request': request, 'plan': plan, 'stage': 'prepared',
                   'employee': employee, 'application': row, 'form': form, 'title': title,
                   'approKey': 'ERP_' + str(uuid.uuid4())}
        write_private(journal_path, journal)
    def save(stage):
        journal['stage'] = stage
        write_private(journal_path, journal, replace=True)
    def write(stage, path, params):
        save(stage + '_attempted')
        try:
            response = client.post(path, params)
        finally:
            journal[stage + 'Response'] = client.last_response
            write_private(journal_path, journal, replace=True)
        journal[stage] = response
        save(stage + '_saved')
        return response
    if journal['stage'] == 'prepared':
        header = dict(coCd='', appDt='', appEmpCd=client.erp['userCode'], deptCd='',
                      titleDc=journal['title'], approLineId='', calLinkKey='', linkKey='',
                      approState='', fileGroup=0, employeeList=[journal['employee']], version='v2',
                      applicationList=[journal['application']])
        write('application', '/human/attendapplication/create', header)
    if not (journal.get('application') or {}).get('appSq'):
        # On disk the calculated row is replaced with the server's creation result.
        raise ValueError('신청정보 저장 응답에 appSq가 없습니다.')
    if journal['stage'] == 'application_saved':
        write('link', '/system/apiUtilEap/GetLinkKey', {'menuCode': 'HPD0110', 'approKey': journal['approKey']})
    link = journal.get('link') or {}
    if not link.get('linkKey') or link.get('approKey') != journal['approKey']:
        raise ValueError('생성된 문서 연결키를 확인할 수 없습니다.')
    if journal['stage'] == 'link_saved':
        form = journal['form']
        write('enage', '/system/apiUtilEap/SetEnageGroup', {
            'approKey': link['approKey'], 'linkKey': link['linkKey'], 'formDTp': form['formDTp'],
            'formId': str(form['formId']), 'formNm': form['formNm'], 'docTitle': journal['title'],
            'contents': '', 'contentsApi': '/human/attendapplication/interlock/getInterlockFormContents',
            'statusApi': '/human/attendapplication/interlock/setInterlockSync', 'dummy1': '', 'link': ''})
    if journal['stage'] == 'enage_saved':
        app = journal['application']
        write('binding', '/human/openapi/attendapplication/saveLinkKey', {
            'linkKey': link['linkKey'], 'appSq': app['appSq'], 'appDt': app['appDt'], 'coCd': app['coCd']})
    if journal['stage'] == 'binding_saved':
        initial = eap_init(client, journal['form']['formId'], link['approKey'])
        body = render_leave(initial, client.uc, journal['title'])
        payload = native_draft_payload(initial, client, journal['title'], link['approKey'], body)
        assert payload['paramItem']['doc_sts'] == '10'
        journal['initial'], journal['payload'] = initial, payload
        try:
            result = write('draft', '/eap/eap110A06', payload)
        except ExpenseError:
            found = client.post('/system/apiUtilEap/GetEnageGroup', {
                'approKey': link['approKey'], 'linkKey': link['linkKey']})
            if not found.get('docId') or str(found.get('approState')) != '5':
                raise
            result = journal['draft'] = {'result': int(found['docId'])}
            save('draft_saved')
        if not result.get('result'):
            raise ValueError('임시저장 문서 번호를 확인할 수 없습니다.')
    if journal['stage'] in ('draft_saved', 'verified'):
        doc_id = int(journal['draft']['result'])
        saved = eap_init(client, journal['form']['formId'], link['approKey'], doc_id)
        info_list = saved['resultMap']['appdocinfo']
        if not isinstance(info_list, list) or len(info_list) != 1:
            raise ValueError('단일 문서 정보를 확인할 수 없습니다.')
        info = info_list[0]
        if str(info['doc_sts']) != '10' or str(info['user_id']) != str(client.uc['empSeq']):
            raise ValueError('본인 임시보관 상태가 아닙니다.')
        if info['approkey'] != link['approKey'] or info['doc_title'] != journal['title']:
            raise ValueError('문서 키 또는 제목이 요청과 다릅니다.')
        content = saved['resultMap']['appdoccontent']
        if len(content) != 1 or urllib.parse.unquote(content[0]['doc_contents']) != urllib.parse.unquote(journal['payload']['paramItem']['doc_contents']):
            raise ValueError('저장된 본문이 요청한 본문과 다릅니다.')
        binding = json.loads(saved['resultMap']['outProcessForm']['outBindData'])
        groups = binding['TABLE']['dbTable1']['group']
        if len(groups) != 1 or str(groups[0]['items']['empCd']) != str(client.erp['userCode']) or len(groups[0]['group']) != 1:
            raise ValueError('휴가 원본 신청자를 확인할 수 없습니다.')
        actual = groups[0]['group'][0]['items']
        expected = journal['plan']['resolved']
        for field, source in [('startDt', 'startDate'), ('endDt', 'endDate'), ('startTm', 'startTime'), ('endTm', 'endTime')]:
            if re.sub(r'[-:]', '', actual[field]) != expected[source]:
                raise ValueError('저장된 휴가 기간/시간이 요청과 다릅니다.')
        if actual['appRmkDc'] != request.get('reason', '') or actual['atCdNm'] != request['type']:
            raise ValueError('저장된 휴가 종류/사유가 요청과 다릅니다.')
        from decimal import Decimal
        if Decimal(str(actual['ycUseCnt'])) != Decimal(str(journal['plan']['calculation']['ycUseCnt'])):
            raise ValueError('저장된 차감 일수가 다릅니다.')
        journal['verifiedBinding'] = actual
        save('verified')
        return {'doc_id': doc_id, 'stage': 'verified', 'calendar_created': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', required=True)
    parser.add_argument('--journal', required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(draft(Client(), read_private(args.request), args.journal), ensure_ascii=False))
    except (ExpenseError, ValueError, KeyError, TypeError) as error:
        raise SystemExit(str(error)) from None
