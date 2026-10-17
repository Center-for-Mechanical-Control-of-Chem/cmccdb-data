"""Lossless CIF evidence/archive and whole-corpus MCP discovery regressions."""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from contextlib import AsyncExitStack
from pathlib import Path
from unittest.mock import patch

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from cmccdb_extraction.contracts import EvidenceRef
from cmccdb_extraction.documents import CIF_MAX_BYTES, digest, index_source
from cmccdb_extraction.pipeline import Pipeline

TEST_ROOT = os.environ.get("CMCCDB_TEST_WORK_ROOT", str(Path.cwd()))
PAPERS = os.environ.get("CMCCDB_TEST_PAPERS")
POPLER = os.environ.get("CMCCDB_POPPLER_BIN")
CIF_BYTES = ("data_fixture\r\n# Source comment\r\n\r\n"
             "_cell_length_a 11.2117(3)\r\n_chemical_name_common\r\n;\r\n"
             " A café\u2028value with original whitespace\r\n;\r\n"
             "loop_\r\n_atom_site_label\r\nNi1\r\n").encode("utf-8")


def pdf_index_fixture(path, poppler_bin=None):
    """Only CIF/archive unit tests substitute PDF parsing; protocol tests use real PDFs."""
    sha = digest(path)
    sid = "src-" + sha[:24]
    return dict(source_id=sid, sha256=sha, filename=path.name, doi_hint=None,
        pages=[dict(evidence_id=sid+":p0001",source_id=sid,page=1,kind="page",
                    width=100,height=100,text="",text_lines=1,needs_visual_reading=False)],
        lines=[dict(evidence_id=sid+":p0001:l0001",source_id=sid,page=1,
                    text="PDF parser fixture",bbox=[0,0,10,10],kind="text")],
        index_version="fixture",coordinate_system="PDF points; top-left origin",
        text_substitutions={},warnings=[])


class CIFIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cif-index-tests-",dir=TEST_ROOT)
        self.root = Path(self.tmp.name)
        self.path = self.root / "structure.cif"
        self.path.write_bytes(CIF_BYTES)

    def tearDown(self):
        self.tmp.cleanup()

    def test_original_bytes_uncertainties_whitespace_and_physical_lines_survive(self):
        source = index_source(self.path)
        self.assertEqual(source["sha256"], hashlib.sha256(CIF_BYTES).hexdigest())
        self.assertEqual(self.path.read_bytes(), CIF_BYTES)
        self.assertEqual(source["source_format"], "cif")
        self.assertEqual(source["index_version"], "cif-utf8-lines-v1")
        self.assertEqual(source["text_substitutions"], {})
        self.assertEqual([r["file_line"] for r in source["lines"]], [1,2,4,5,6,7,8,9,10,11])
        self.assertEqual(source["lines"][2]["text"], "_cell_length_a 11.2117(3)")
        self.assertEqual(source["lines"][5]["text"], " A café\u2028value with original whitespace")
        for row in source["lines"]:
            self.assertEqual(row["evidence_id"], source["source_id"]+f":p0001:l{row['file_line']:06d}")
            self.assertIsNone(row["bbox"])
        self.assertEqual(source["pages"][0]["kind"], "text_file")
        self.assertNotIn("width", source["pages"][0])
        self.assertIn("not parsed or executed",source["coordinate_system"])
        self.assertNotIn("crystal_parameters",source)
        self.assertNotIn("smiles",source)

    def test_utf8_binary_empty_and_control_errors_are_explicit(self):
        for body,message in [(b"data_x\n\xff", "valid UTF-8"),
                             (b"data_x\n\x00", "control characters"),
                             (b"data_x\n\x0b", "control characters"),
                             (b" \r\n\t\n", "no nonblank")]:
            with self.subTest(body=body):
                self.path.write_bytes(body)
                with self.assertRaisesRegex(ValueError,message):
                    index_source(self.path)
                self.assertEqual(self.path.read_bytes(),body)

    def test_size_budget_is_checked_before_decoding(self):
        with self.path.open("wb") as stream:
            stream.truncate(CIF_MAX_BYTES+1)
        with self.assertRaisesRegex(ValueError,"20 MiB"):
            index_source(self.path)

    def test_line_budget_counts_physical_lines_and_terminal_newline_correctly(self):
        with patch("cmccdb_extraction.documents.CIF_MAX_LINES",3):
            self.path.write_bytes(b"data_x\r\n# original\r\n_cell_length_a 1\r\n")
            self.assertEqual(len(index_source(self.path)["lines"]),3)
            self.path.write_bytes(b"data_x\n\n\n\n_cell_length_a 1")
            with self.assertRaisesRegex(ValueError,"line indexing limit"):
                index_source(self.path)


class CIFArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cif-archive-tests-",dir=TEST_ROOT)
        self.root = Path(self.tmp.name)
        self.papers = self.root / "papers"
        self.papers.mkdir()
        (self.papers / "article.pdf").write_bytes(b"PDF parser unit fixture")
        self.cif = self.papers / "structure.cif"
        self.cif.write_bytes(CIF_BYTES)
        self.pipeline = Pipeline([self.papers],self.root/"runs")
        self.pdf_mock = patch("cmccdb_extraction.documents.index_pdf",side_effect=pdf_index_fixture)
        self.pdf_mock.start()

    def tearDown(self):
        self.pdf_mock.stop()
        self.tmp.cleanup()

    def test_cif_is_supporting_source_and_archive_is_independent_of_original_changes(self):
        run = self.pipeline.create_run("article.pdf",["structure.cif"],idempotency_key="original")
        source = run["sources"][1]
        self.assertEqual(source["role"],"supporting_information")
        self.assertEqual(source["source_format"],"cif")
        self.assertEqual(source["archive_extension"],".cif")
        index,path = self.pipeline.index(run["run_id"],source["source_id"])
        self.assertEqual(path.read_bytes(),CIF_BYTES)
        self.cif.write_bytes(b"data_changed\n_cell_length_a 99\n")
        reloaded,archive = self.pipeline.index(run["run_id"],source["source_id"])
        self.assertEqual(reloaded,index)
        self.assertEqual(archive.read_bytes(),CIF_BYTES)
        with self.assertRaisesRegex(ValueError,"different inputs"):
            self.pipeline.create_run("article.pdf",["structure.cif"],idempotency_key="original")
        archive.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError,"checksum"):
            self.pipeline.index(run["run_id"],source["source_id"])

    def test_exact_cif_claim_citations_work_and_fabricated_quotes_or_pixels_fail(self):
        run=self.pipeline.create_run("article.pdf",["structure.cif"])
        source=run["sources"][1]
        index,_=self.pipeline.index(run["run_id"],source["source_id"])
        row=next(r for r in index["lines"] if r["file_line"]==4)
        ref=EvidenceRef(evidence_id=row["evidence_id"],quote="11.2117(3)")
        self.assertEqual(self.pipeline.checked_reference(run["run_id"],ref)["file_line"],4)
        with self.assertRaisesRegex(ValueError,"does not occur"):
            self.pipeline.checked_reference(run["run_id"],EvidenceRef(evidence_id=row["evidence_id"],quote="invented lattice"))
        with self.assertRaisesRegex(ValueError,"unavailable"):
            self.pipeline.page(run["run_id"],source["source_id"],1)
        self.assertEqual(self.pipeline.search(run["run_id"],"11.2117",source["source_id"])["matches"],[row])

    def test_invalid_cif_ingestion_rolls_back_and_cif_cannot_replace_main_pdf(self):
        self.cif.write_bytes(b"data_bad\n\xff")
        with self.assertRaisesRegex(ValueError,"valid UTF-8"):
            self.pipeline.create_run("article.pdf",["structure.cif"])
        self.assertEqual(list((self.root/"runs").glob("run-*")),[])
        with self.pipeline.store.connect() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM runs").fetchone()[0],0)
        with self.assertRaisesRegex(ValueError,"main article must be a PDF"):
            self.pipeline.create_run("structure.cif")

    def test_cif_root_escape_and_source_changes_during_ingestion_are_rejected(self):
        outside=self.root/"outside.cif"
        outside.write_bytes(CIF_BYTES)
        with self.assertRaisesRegex(ValueError,"configured paper root"):
            self.pipeline.create_run("article.pdf",[str(outside)])
        (self.papers/"escape.cif").symlink_to(outside)
        with self.assertRaisesRegex(ValueError,"configured paper root"):
            self.pipeline.create_run("article.pdf",["escape.cif"])
        real_index=index_source
        def changed_index(path,poppler_bin=None):
            if path.suffix==".cif":
                path.write_bytes(b"data_changed\n")
            return real_index(path,poppler_bin)
        with patch("cmccdb_extraction.pipeline.index_source",side_effect=changed_index):
            with self.assertRaisesRegex(ValueError,"Source changed during ingestion"):
                self.pipeline.create_run("article.pdf",["structure.cif"])


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix="paper-discovery-tests-",dir=TEST_ROOT)
        self.root=Path(self.tmp.name)
        self.papers=self.root/"papers"
        self.papers.mkdir()
        for number in range(515):
            (self.papers/f"paper-{number:04d}.pdf").write_bytes(b"Discovery fixture; not parsed")
        self.pipeline=Pipeline([self.papers],self.root/"runs")

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_and_next_page_cover_all_515_without_duplicates(self):
        first=self.pipeline.list_papers()
        self.assertEqual(len(first["papers"]),500)
        self.assertEqual(first["total"],515)
        self.assertEqual(first["next_offset"],500)
        second=self.pipeline.list_papers(offset=first["next_offset"])
        self.assertEqual(len(second["papers"]),15)
        self.assertIsNone(second["next_offset"])
        paths=[r["path"] for r in first["papers"]+second["papers"]]
        self.assertEqual(paths,[str(p) for p in sorted(self.papers.glob("*.pdf"))])
        self.assertEqual(len(set(paths)),515)
        self.assertEqual(self.pipeline.list_papers(offset=600),dict(papers=[],total=515,next_offset=None))

    def test_bounds_types_and_escape_are_enforced(self):
        for offset,limit in [(-1,1),(0,0),(0,501),(True,1),(0,False),(1.5,1),(0,"1")]:
            with self.subTest(offset=offset,limit=limit),self.assertRaises(ValueError):
                self.pipeline.list_papers(offset=offset,limit=limit)
        outside=self.root/"outside.pdf"
        outside.write_bytes(b"Outside root")
        (self.papers/"escaped.pdf").symlink_to(outside)
        self.assertEqual(self.pipeline.list_papers(limit=1)["total"],515)


async def call(session,name,**arguments):
    result=await session.call_tool(name,arguments)
    if result.is_error:
        raise ValueError("; ".join(c.text for c in result.content if hasattr(c,"text")))
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(next(c.text for c in result.content if hasattr(c,"text")))


class PublicMCPTests(unittest.IsolatedAsyncioTestCase):
    async def connect(self,stack,papers,runs):
        args=["-m","cmccdb_extraction.server","--paper-root",str(papers),"--work-root",str(runs)]
        if POPLER:args += ["--poppler-bin",POPLER]
        read,write=await stack.enter_async_context(stdio_client(StdioServerParameters(
            command=sys.executable,args=args,env=dict(os.environ))))
        session=await stack.enter_async_context(ClientSession(read,write))
        await session.initialize()
        return session

    async def test_discovery_pagination_and_argument_checks_over_stdio(self):
        with tempfile.TemporaryDirectory(prefix="mcp-discovery-tests-",dir=TEST_ROOT) as directory:
            root=Path(directory);papers=root/"papers";papers.mkdir()
            for number in range(515):
                (papers/f"paper-{number:04d}.pdf").write_bytes(b"Discovery fixture; not parsed")
            async with AsyncExitStack() as stack:
                session=await self.connect(stack,papers,root/"runs")
                tool=next(t for t in (await session.list_tools()).tools if t.name=="list_papers")
                self.assertEqual(tool.input_schema["properties"]["offset"]["default"],0)
                self.assertEqual(tool.input_schema["properties"]["limit"]["default"],500)
                first=await call(session,"list_papers")
                last=await call(session,"list_papers",offset=first["next_offset"])
                self.assertEqual((len(first["papers"]),len(last["papers"]),last["next_offset"]),(500,15,None))
                for arguments in [dict(offset=-1),dict(limit=0),dict(limit=501),dict(offset=True)]:
                    with self.subTest(arguments=arguments):
                        self.assertTrue((await session.call_tool("list_papers",arguments)).is_error)

    @unittest.skipUnless(PAPERS and POPLER,"Set CMCCDB_TEST_PAPERS/CMCCDB_POPPLER_BIN for real-PDF protocol archive check")
    async def test_native_cif_create_read_search_and_restart_over_stdio(self):
        import shutil
        with tempfile.TemporaryDirectory(prefix="mcp-cif-tests-",dir=TEST_ROOT) as directory:
            root=Path(directory);papers=root/"papers";papers.mkdir()
            shutil.copyfile(Path(PAPERS)/"d6mr00048g.pdf",papers/"article.pdf")
            (papers/"structure.cif").write_bytes(CIF_BYTES)
            async with AsyncExitStack() as stack:
                session=await self.connect(stack,papers,root/"runs")
                run=await call(session,"create_run",paper="article.pdf",supporting_information=["structure.cif"])
                source=run["sources"][1]
                self.assertEqual(source["source_format"],"cif")
                page=await call(session,"read_source_page",run_id=run["run_id"],source_id=source["source_id"],page=1,limit=3)
                self.assertEqual(page["next_offset"],3)
                matches=await call(session,"search_evidence",run_id=run["run_id"],source_id=source["source_id"],query="11.2117")
                row=matches["matches"][0]
                exact=await call(session,"read_evidence",run_id=run["run_id"],evidence_ids=[row["evidence_id"]])
                self.assertEqual(exact["evidence"],[row])
                self.assertTrue((await session.call_tool("view_page",dict(run_id=run["run_id"],source_id=source["source_id"],page=1))).is_error)
            (papers/"structure.cif").write_bytes(b"data_edited_original\n")
            async with AsyncExitStack() as restarted:
                session=await self.connect(restarted,papers,root/"runs")
                exact=await call(session,"read_evidence",run_id=run["run_id"],evidence_ids=[row["evidence_id"]])
                self.assertEqual(exact["evidence"],[row])


if __name__=="__main__":
    unittest.main()
