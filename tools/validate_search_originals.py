"""Published DOI + arXiv URL must preserve the original-paper download chain."""
import json
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from unittest.mock import patch
import unittest
import fitz
from paperpilot import downloader, library, repo_manager

class Originals(unittest.TestCase):
    def test_identifier_contract_and_unrelated_publisher_records(self):
        resolve=downloader._arxiv_id_from_paper
        cases=[({'doi':'10.48550/arXiv.1706.03762'},'1706.03762'),
               ({'doi':'https://doi.org/10.48550/arXiv.hep-th/9901001v2'},'hep-th/9901001'),
               ({'doi':'10.1038/2024.01234','url':'https://arxiv.org/abs/2401.00001v2'},'2401.00001'),
               ({'url':'https://arxiv.org/html/2401.00001v2'},'2401.00001'),
               ({'doi':'10.1038/2024.01234','url':'https://nature.com/articles/2024.01234'},None),
               ({'url':'https://example.com/abs/2401.00001'},None),
               ({'url':'https://arxiv.org/abs/2400.00001'},None),
               ({},None)]
        for record,expected in cases:
            with self.subTest(record=record): self.assertEqual(resolve(record),expected)

    def test_search_record_published_doi_and_arxiv_url_pdf_html_archive(self):
        document=fitz.open();document.new_page().insert_text((50,100),"Original scientific paper evidence. "*4)
        data=document.tobytes();document.close();calls=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                calls.append(self.path)
                html=b'<html><title>Original paper</title><main>'+b'<p>Reproducible scientific results.</p>'*40+b'</main></html>'
                payload=html if self.path.startswith('/html/') else data
                self.send_response(200);self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{server.server_port}'
        try:
            with patch.object(downloader,'_ARXIV_PDF',base+'/pdf/{}'), \
                 patch.object(downloader,'_ARXIV_HTML',base+'/html/{}'), \
                 patch.object(downloader,'_new_session',return_value=(None,'')):
                project=library.create_project('Original workflow','Study the original paper')
                for i,url in enumerate(('https://arxiv.org/abs/2401.00001v2',
                                        'https://arxiv.org/pdf/hep-th/9901001')):
                    paper=dict(title=f'Original paper {i}',source='arxiv',url=url,
                               doi=f'10.1038/s41586-test-{i}',abstract='Detailed abstract. '*20,year=2024)
                    before=dict(paper)
                    assert library.save_papers_to_project(project.id,[paper])[0]==1
                    pdf=downloader.cache_pdf(paper);self.assertTrue(pdf and Path(pdf).is_file(),msg=f'{url}: {calls}')
                    self.assertIn('Original scientific paper',downloader.extract_pdf_text(Path(pdf).read_bytes()))
                    paper['pdf_path']=pdf
                    archived=repo_manager.import_pdf(paper,project.name);self.assertTrue(archived and Path(archived).is_file())
                    self.assertTrue(library.set_paper_pdf_path_smart(paper,archived))
                    html=downloader.fetch_full_text(paper);self.assertTrue(html and Path(html).is_file())
                    self.assertIn('Reproducible scientific results',Path(html).read_text(encoding='utf-8'))
                    self.assertEqual(paper['doi'],before['doi']);self.assertEqual(paper['url'],before['url'])
                stored=library.get_project_papers(project.id)
                self.assertTrue(all(p['doi'].startswith('10.1038/') and Path(p['pdf_path']).is_file() for p in stored))
                self.assertTrue(all(path.startswith(('/pdf/','/html/')) for path in calls))
                (Path.cwd()/'originals_report.json').write_text(json.dumps(dict(passed=True,transport='real local HTTP',
                    preserved_publisher_dois=True,arxiv_ids=['2401.00001v2','hep-th/9901001'],calls=calls)),encoding='utf-8')
        finally:
            server.shutdown();server.server_close();thread.join(2)

if __name__=='__main__': unittest.main(verbosity=2)
