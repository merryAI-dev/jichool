"""Offline replay: python3 test_leave_draft.py PRIVATE_JOURNAL PRIVATE_SAVED_EAP"""
import copy
import json
from pathlib import Path
import sys
import tempfile

from expense import write_private
from leave_draft import draft, remove_embedded_reset


if __name__ == '__main__':
    assert remove_embedded_reset('<style>* {margin:0;padding:0;}</style><p>휴가</p>') == '<p>휴가</p>'
    journal = json.loads(Path(sys.argv[1]).read_text())
    saved = json.loads(Path(sys.argv[2]).read_text())
    class ReadOnlyReplay:
        erp = {'companyCode': journal['account'][1], 'userCode': journal['account'][2]}
        uc = {'groupSeq': journal['account'][0], 'empSeq': saved['resultMap']['appdocinfo'][0]['user_id']}

        def post(self, path, params):
            if path == '/system/apiUtilEap/GetEnageGroup':
                return {'docId': saved['resultMap']['appdocinfo'][0]['doc_id'], 'approState': '5'}
            assert path == '/eap/eap110A03', 'Recovery must not repeat a write'
            return saved
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / 'draft.json'
        original = copy.deepcopy(journal)
        original['stage'] = 'draft_attempted'
        write_private(target, original)
        result = draft(ReadOnlyReplay(), journal['request'], target)
        assert result['stage'] == 'verified' and not result['calendar_created']
        saved['resultMap']['appdoccontent'][0]['doc_contents'] += 'unexpected change'
        try:
            draft(ReadOnlyReplay(), journal['request'], target)
        except ValueError as error:
            assert '본문' in str(error)
        else:
            raise AssertionError('Changed content accepted')
    print('Actual draft replay: lost-response recovery and content mismatch checks passed; no remote writes')
