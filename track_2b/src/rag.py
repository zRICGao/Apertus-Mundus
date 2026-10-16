from database import connect_db, atomic
from providers import generation, parse_object
"""
Z.R.I.C 引擎 — RAG 知识库模块 (rag.py)
从 main.py 中抽离的 RAG 相关：切片、embedding、检索、REST API。
由 main.py 通过 app.include_router(rag_router) 挂载。
"""

import math
import json
import time
import hashlib
from datetime import datetime, timezone
from fastapi import APIRouter, UploadFile, File, Form
from pydantic import BaseModel, Field
from world import visible, validate_visibility, refresh_wiki
from logger import get_logger

_log = get_logger("rag")

rag_router = APIRouter(tags=["RAG 知识库"])

# ---------------------------------------------------------
# 数据库连接（由 main.py 注入）
# ---------------------------------------------------------
_db_file: str = ""
_embed_client = None  # OpenAI-compatible embedding client

# RAG 参数
RAG_CHUNK_SIZE    = 600
RAG_CHUNK_OVERLAP = 80
RAG_TOP_K         = 6

# 硅基流动 Embedding 配置
SILICONFLOW_EMB_MODEL = "BAAI/bge-m3"


def configure_rag(db_file, siliconflow_api_key="", siliconflow_base_url=""):
    global _db_file, _embed_client, SILICONFLOW_EMB_MODEL
    _db_file, _embed_client = db_file, None
    settings = generation.settings
    SILICONFLOW_EMB_MODEL = settings.embedding_model
    base = settings.embedding_url
    if settings.embedding_enabled and (settings.embedding_key or base.startswith(("http://localhost:", "http://127.0.0.1:", "http://[::1]:"))):
        from openai import OpenAI
        _embed_client = OpenAI(api_key=settings.embedding_key or "local", base_url=base,timeout=20,max_retries=0)


def get_db_connection():
    return connect_db(_db_file)


# ---------------------------------------------------------
# 数据库表初始化
# ---------------------------------------------------------
def init_rag_tables():
    """创建 RAG 相关表（幂等）。由 main.py 的 init_db() 调用。"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS rag_documents (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        title      TEXT NOT NULL,
        source     TEXT NOT NULL DEFAULT '',
        chunk_size INTEGER NOT NULL DEFAULT 0,
        hidden     INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')
    # 迁移：为旧数据库添加 hidden 列
    try:
        cursor.execute("ALTER TABLE rag_documents ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    except Exception:
        pass
    cursor.execute('''CREATE TABLE IF NOT EXISTS rag_chunks (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        doc_id      INTEGER NOT NULL,
        chunk_index INTEGER NOT NULL DEFAULT 0,
        chunk_text  TEXT NOT NULL,
        embedding   TEXT NOT NULL DEFAULT '[]',
        FOREIGN KEY(doc_id) REFERENCES rag_documents(id) ON DELETE CASCADE
    )''')
    for column,kind in {"visibility":"TEXT NOT NULL DEFAULT 'public'", "timeline_id":"INTEGER",
                         "embedding_model":"TEXT NOT NULL DEFAULT ''", "raw_text":"TEXT NOT NULL DEFAULT ''"}.items():
        if column not in {r['name'] for r in conn.execute('PRAGMA table_info(rag_documents)')}:
            conn.execute(f'ALTER TABLE rag_documents ADD COLUMN {column} {kind}')
    for column,kind in {"embedding_meta":"TEXT NOT NULL DEFAULT '{}'"}.items():
        if column not in {r['name'] for r in conn.execute('PRAGMA table_info(rag_chunks)')}:
            conn.execute(f'ALTER TABLE rag_chunks ADD COLUMN {column} {kind}')
    conn.commit()
    conn.close()


# ---------------------------------------------------------
# Pydantic 模型
# ---------------------------------------------------------
class RagIngestRequest(BaseModel):
    title:        str
    source:       str = ""
    text:         str
    chunk_size:   int = RAG_CHUNK_SIZE
    chunk_overlap: int = RAG_CHUNK_OVERLAP
    hidden:       int = 0
    visibility: str = "public"
    timeline_id: int | None = None


class RagSearchRequest(BaseModel):
    scene_name: str
    content:    str
    viewer: str = "public"
    timeline_id: int | None = None
    top_k:      int = 0  # 0 = 使用默认值 RAG_TOP_K*2


# ---------------------------------------------------------
# 核心函数：切片、embedding、检索
# ---------------------------------------------------------
import re

def chunk_text(text, max_size=600, overlap=80):
    if max_size < 100 or max_size > 8000 or overlap < 0 or overlap >= max_size:
        raise ValueError("切片长度需在100至8000之间，重叠必须小于长度")
    text, chunks, start = text.strip(), [], 0
    while start < len(text):
        end=min(start+max_size,len(text))
        if end<len(text):
            boundary=max(text.rfind(c,start+max_size//2,end) for c in ('\n','。','！','？','. '))
            if boundary>=0: end=boundary+1
        chunks.append(text[start:end])
        if end==len(text): break
        start=max(start+1,end-overlap)
    return chunks


def get_embeddings(texts):
    if not texts or _embed_client is None: return []
    vectors=[]
    for offset in range(0,len(texts),16):
        batch=texts[offset:offset+16]
        try:
            result=_embed_client.embeddings.create(model=generation.settings.embedding_model,input=batch)
            items=sorted(result.data,key=lambda x:x.index)
            vs=[i.embedding for i in items]
            if len(vs)!=len(batch) or any(not v or any(type(x) not in {int,float} or not math.isfinite(x) for x in v) for v in vs):
                raise ValueError("无效向量")
            vectors.extend(vs)
        except Exception:
            vectors.extend([] for _ in batch)
    return vectors


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """纯 Python 余弦相似度。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot   = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ---------------------------------------------------------
# 可见性和向量模型元数据在同一检索中检查
# ---------------------------------------------------------
# 全局缓存实例


def refresh_vector_cache():
    # Current retrieval loads vector and visibility metadata together to avoid cache leakage.
    return None


def vector_metadata(text, vector):
    return json.dumps({'model':generation.settings.embedding_model,
        'service':hashlib.sha256(generation.settings.embedding_url.rstrip('/').encode()).hexdigest(),
        'dimensions':len(vector),'content_hash':hashlib.sha256(text.encode()).hexdigest(),
        'chunker':'paragraph-v1','created_at':datetime.now(timezone.utc).isoformat()}) if vector else '{}'


def embedding_identity():
    return (generation.settings.embedding_model, generation.settings.embedding_url)


def complete_vectors(vectors, count):
    return (len(vectors)==count and bool(vectors) and
            all(isinstance(v,list) and v and all(type(x) in (int,float) and math.isfinite(x) for x in v) for v in vectors) and
            len({len(v) for v in vectors})==1)


def vector_valid(row, vector):
    try:
        metadata=json.loads(row['embedding_meta'])
        return (row['embedding_model']==generation.settings.embedding_model and
                metadata.get('model')==generation.settings.embedding_model and
                metadata.get('service')==hashlib.sha256(generation.settings.embedding_url.rstrip('/').encode()).hexdigest() and
                metadata.get('content_hash')==hashlib.sha256(row['chunk_text'].encode()).hexdigest() and
                metadata.get('dimensions')==len(vector) and bool(vector) and
                all(type(x) in (int,float) and math.isfinite(x) for x in vector))
    except (ValueError,TypeError,KeyError):
        return False


# ---------------------------------------------------------
# 核心检索函数（使用向量缓存加速）
# ---------------------------------------------------------


# ---------------------------------------------------------
# 混合检索核心（关键词预匹配 + 向量语义检索）
# ---------------------------------------------------------
def _hybrid_retrieve(conn,query_text,top_k=6,vec_threshold=.25,*,viewer="gm",timeline_id=None,sources=None,doc_ids=None,candidate_cap=30):
    terms=set(re.findall(r'[a-z0-9_]+',query_text.lower()))
    for seq in re.findall(r'[\u4e00-\u9fff]+',query_text):
        terms.update(seq[i:i+2] for i in range(len(seq)-1))
        if len(seq)==1: terms.add(seq)
    # Eligibility precedes scoring and even the optional query-embedding request.
    from knowledge import allowed
    eligible=[]
    for row in conn.execute("SELECT c.*,d.title,d.source,d.visibility,d.hidden,d.timeline_id,d.embedding_model FROM rag_chunks c JOIN rag_documents d ON d.id=c.doc_id"):
        if doc_ids is not None and row['doc_id'] not in doc_ids:continue
        if sources is not None and ('history' if row['source']=='memory_l3_eviction' else 'lore') not in sources:continue
        if row['hidden'] or not allowed(row['visibility'],row['timeline_id'],viewer,timeline_id):continue
        eligible.append(row)
    if not eligible:return []
    query_vectors=get_embeddings([query_text])
    query_vector=query_vectors[0] if query_vectors else []
    scored=[]
    for row in eligible:
        text=(row['title']+' '+row['chunk_text']).lower()
        lexical=sum(t in text for t in terms)/max(1,len(terms))
        dense=0
        if query_vector and row['embedding_model']==generation.settings.embedding_model:
            try:
                vector=json.loads(row['embedding'])
                if vector_valid(row,vector): dense=cosine_similarity(query_vector,vector)
            except (ValueError,TypeError): pass
        if lexical or dense>max(.05,vec_threshold):
            score=lexical+max(0,dense)*.7
            scored.append((score,{**dict(row),'score':round(score,4)}))
    return [r for _,r in sorted(scored,key=lambda x:-x[0])[:max(1,min(top_k,candidate_cap,200))]]


def rag_retrieve(conn, query_text: str, top_k: int = RAG_TOP_K, *, viewer="gm", timeline_id=None,sources=None) -> str:
    """
    检索最相关的 top_k 个切片，格式化为 prompt 注入文本。
    使用混合检索：关键词精确匹配置顶 + 向量语义检索补充。
    """
    results = _hybrid_retrieve(conn, query_text, top_k=top_k,viewer=viewer,timeline_id=timeline_id,sources=sources)
    if not results:
        return ""
    return "\n\n".join(
        f"[来源：{r['title']}]\n{r['chunk_text']}" for r in results
    )


# ---------------------------------------------------------
# REST API 端点
# ---------------------------------------------------------
@rag_router.get("/api/rag/documents")
def rag_list_documents():
    conn = get_db_connection()
    docs = conn.execute("SELECT * FROM rag_documents ORDER BY created_at DESC").fetchall()
    result = []
    for d in docs:
        chunk_count = conn.execute(
            "SELECT COUNT(*) as c FROM rag_chunks WHERE doc_id=?", (d["id"],)
        ).fetchone()["c"]
        result.append({**dict(d), "chunk_count": chunk_count})
    conn.close()
    return {"status": "success", "documents": result}


@rag_router.post("/api/rag/ingest")
def rag_ingest(req: RagIngestRequest):
    from fastapi import HTTPException
    try:
        validate_visibility(req.visibility)
        chunks=chunk_text(req.text,req.chunk_size,req.chunk_overlap)
        if not chunks: raise ValueError("文本不能为空")
    except ValueError as exc: raise HTTPException(422,str(exc)) from exc
    identity=embedding_identity()
    vectors=get_embeddings(chunks)
    conn=get_db_connection()
    try:
        with atomic(conn):
            doc=conn.execute("INSERT INTO rag_documents(title,source,chunk_size,hidden,visibility,timeline_id,embedding_model) VALUES(?,?,?,?,?,?,?)",
                (req.title[:100],req.source[:200],len(chunks),bool(req.hidden),req.visibility,req.timeline_id,generation.settings.embedding_model if vectors else '')).lastrowid
            conn.execute('UPDATE rag_documents SET raw_text=? WHERE id=?',(req.text,doc))
            complete = complete_vectors(vectors,len(chunks)) and identity==embedding_identity()
            if not complete:
                vectors=[]
                conn.execute("UPDATE rag_documents SET embedding_model='' WHERE id=?",(doc,))
            for i,chunk in enumerate(chunks):
                vector=vectors[i] if i<len(vectors) else []
                conn.execute("INSERT INTO rag_chunks(doc_id,chunk_index,chunk_text,embedding,embedding_meta) VALUES(?,?,?,?,?)",(doc,i,chunk,json.dumps(vector),vector_metadata(chunk,vector)))
            refresh_wiki(conn)
        return {"status":"success","doc_id":doc,"chunk_count":len(chunks),"embedded":sum(bool(v) for v in vectors)}
    finally: conn.close()


@rag_router.post("/api/rag/upload")
async def rag_upload(
    file:          UploadFile = File(...),
    title:         str        = Form(""),
    source:        str        = Form(""),
    chunk_size:    int        = Form(RAG_CHUNK_SIZE),
    chunk_overlap: int        = Form(RAG_CHUNK_OVERLAP),
    hidden:        int        = Form(0),
    visibility: str = Form("public"),
    timeline_id: int | None = Form(None),
):
    """上传 .txt / .pdf 文件并导入知识库。"""
    t0 = time.time()
    filename = file.filename or "unknown"
    raw = await file.read()

    # ── 提取文本 ──────────────────────────────────────────
    if filename.lower().endswith(".pdf"):
        try:
            import io
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(raw))
            text = "\n".join(p.extract_text() or "" for p in reader.pages)
        except ImportError:
            return {"status": "error", "message": "PDF 解析需要安装 pypdf：pip install pypdf"}
        except Exception as e:
            return {"status": "error", "message": f"PDF 解析失败：{e}"}
    else:
        # 尝试 UTF-8，fallback GBK
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = raw.decode("gbk")
            except Exception:
                return {"status": "error", "message": "文件编码无法识别，请转为 UTF-8 后重试"}

    text = text.strip()
    if not text:
        return {"status": "error", "message": "文件内容为空"}

    doc_title  = (title.strip()  or filename)[:100]
    doc_source = (source.strip() or filename)[:200]

    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(rag_ingest, RagIngestRequest(title=doc_title,source=doc_source,text=text,
                chunk_size=chunk_size,chunk_overlap=chunk_overlap,hidden=hidden,visibility=visibility,timeline_id=timeline_id))


@rag_router.delete("/api/rag/documents/{doc_id}")
def rag_delete_document(doc_id: int):
    conn = get_db_connection()
    conn.execute("DELETE FROM rag_chunks WHERE doc_id=?", (doc_id,))
    conn.execute("DELETE FROM rag_documents WHERE id=?", (doc_id,))
    refresh_wiki(conn)
    conn.commit(); conn.close()
    # 删除后刷新缓存
    refresh_vector_cache()
    return {"status": "success"}


@rag_router.patch("/api/rag/documents/{doc_id}/hidden")
def rag_toggle_hidden(doc_id: int, hidden: int):
    """设置文档的隐藏状态（hidden=0 公开，hidden=1 隐藏）。"""
    conn = get_db_connection()
    conn.execute("UPDATE rag_documents SET hidden=? WHERE id=?", (1 if hidden else 0, doc_id))
    refresh_wiki(conn)
    conn.commit(); conn.close()
    refresh_vector_cache()
    return {"status": "success", "doc_id": doc_id, "hidden": hidden}


@rag_router.post("/api/rag/search")
def rag_search(req: RagSearchRequest):
    """独立检索接口，供前端测试知识库效果。使用混合检索。"""
    query = f"{req.scene_name} {req.content}"
    conn = get_db_connection()
    effective_top_k = req.top_k if req.top_k > 0 else RAG_TOP_K * 2
    results = _hybrid_retrieve(conn, query, top_k=effective_top_k, vec_threshold=0.0, viewer=req.viewer,timeline_id=req.timeline_id)
    conn.close()
    return {"status": "success", "results": results}


@rag_router.post('/api/rag/reindex')
def rag_reindex():
    from fastapi import HTTPException
    if _embed_client is None:
        return {'status':'skipped','message':'尚未启用向量服务，关键词检索仍可用'}
    conn=get_db_connection()
    try:
        rows=conn.execute('SELECT id,doc_id,chunk_text FROM rag_chunks ORDER BY id').fetchall()
        epoch=conn.execute("SELECT value FROM system_state WHERE key='world_epoch'").fetchone()[0]
        identity=embedding_identity()
        vectors=get_embeddings([r['chunk_text'] for r in rows])
        if not rows:
            return {'status':'success','embedded':0}
        if not complete_vectors(vectors,len(rows)):
            raise HTTPException(503,'向量服务未返回完整有效结果，保留原索引')
        with atomic(conn):
            if identity!=embedding_identity():
                raise HTTPException(409,'向量服务配置已变化，请重试')
            if conn.execute("SELECT value FROM system_state WHERE key='world_epoch'").fetchone()[0]!=epoch:
                raise HTTPException(409,'世界已更换，请重试')
            current=conn.execute('SELECT id,doc_id,chunk_text FROM rag_chunks ORDER BY id').fetchall()
            if [tuple(r) for r in current]!=[tuple(r) for r in rows]:
                raise HTTPException(409,'资料已变化，未发布过期向量')
            for row,vector in zip(rows,vectors):
                conn.execute('UPDATE rag_chunks SET embedding=?,embedding_meta=? WHERE id=? AND chunk_text=?',
                             (json.dumps(vector),vector_metadata(row['chunk_text'],vector),row['id'],row['chunk_text']))
            for doc_id in {r['doc_id'] for r in rows}:
                conn.execute('UPDATE rag_documents SET embedding_model=? WHERE id=?',(generation.settings.embedding_model,doc_id))
            refresh_wiki(conn)
        return {'status':'success','embedded':sum(bool(v) for v in vectors)}
    finally: conn.close()
