"""Private Docker endpoints for BGE-M3, MXBAI Base v2, and the upstream Kev server."""
from contextlib import asynccontextmanager
import base64
import json
import os
from pathlib import Path
import sys
import struct
import threading

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

ROLE = os.environ.get('HELPER_ROLE', 'embedding')
MODELS = json.loads(Path(__file__).with_name('models.json').read_text())
lock = threading.Lock()
model = None


def load_model():
    import torch
    torch.set_num_threads(int(os.environ.get('HELPER_THREADS', '4')))
    paths = json.loads(Path('/opt/models/paths.json').read_text())
    device = os.environ.get('HELPER_DEVICE', 'cpu')
    if ROLE == 'embedding':
        from sentence_transformers import SentenceTransformer
        return SentenceTransformer(paths[ROLE], device=device, local_files_only=True)
    from sentence_transformers import CrossEncoder
    return CrossEncoder(paths[ROLE], device=device, local_files_only=True,
                        max_length=int(os.environ.get('RERANK_MAX_LENGTH', '2048')))


@asynccontextmanager
async def lifespan(app):
    global model
    model = load_model()
    yield
    model = None


app = FastAPI(title='Apertus Mundus local ' + ROLE, lifespan=lifespan)


class EmbeddingRequest(BaseModel):
    model: str
    input: str | list[str]
    encoding_format: str = 'float'


class RerankRequest(BaseModel):
    model: str
    query: str = Field(min_length=1, max_length=12000)
    documents: list[str] = Field(min_length=1, max_length=80)
    top_n: int | None = Field(default=None, ge=1)
    return_documents: bool = False


@app.get('/health')
def health():
    if model is None:
        raise HTTPException(503, 'Model is loading')
    return {'status': 'ok', 'model': MODELS[ROLE]['repo']}


@app.post('/v1/embeddings')
def embeddings(req: EmbeddingRequest):
    if ROLE != 'embedding' or req.model != MODELS[ROLE]['repo']:
        raise HTTPException(404, 'Model not served here')
    texts = [req.input] if isinstance(req.input, str) else req.input
    if not 1 <= len(texts) <= 32 or any(not text or len(text) > 32000 for text in texts):
        raise HTTPException(422, 'Expected 1-32 nonempty texts of at most 32000 characters')
    if req.encoding_format not in {'float', 'base64'}:
        raise HTTPException(422, 'Expected float or base64 embeddings')
    with lock:
        vectors = model.encode(texts, batch_size=4, normalize_embeddings=True, show_progress_bar=False).tolist()
    if req.encoding_format == 'base64':
        vectors = [base64.b64encode(struct.pack('<' + 'f' * len(vector), *vector)).decode('ascii') for vector in vectors]
    return {'object': 'list', 'model': req.model,
            'data': [{'object': 'embedding', 'index': index, 'embedding': vector}
                     for index, vector in enumerate(vectors)]}


@app.post('/rerank')
def rerank(req: RerankRequest):
    if ROLE != 'reranker' or req.model != MODELS[ROLE]['repo']:
        raise HTTPException(404, 'Model not served here')
    if any(not document or len(document) > 12000 for document in req.documents):
        raise HTTPException(422, 'Documents must contain 1-12000 characters')
    with lock:
        scores = model.predict([(req.query, document) for document in req.documents],
                               batch_size=4, show_progress_bar=False).reshape(-1).tolist()
    indices = sorted(range(len(scores)), key=lambda index: (-scores[index], index))[:req.top_n]
    return {'model': req.model, 'results': [dict(index=index, relevance_score=float(scores[index]),
             **({'document': {'text': req.documents[index]}} if req.return_documents else {})) for index in indices]}


def main():
    if ROLE == 'kev':
        # Keep the upstream System One implementation, calibration and pointer head.
        spec = MODELS['kev']
        os.execv(sys.executable, [sys.executable, '-m', 'kev.serve', '--run',
                 spec['repo'] + '@' + spec['revision'], '--host', '0.0.0.0', '--port', '8008',
                 '--device', os.environ.get('HELPER_DEVICE', 'cpu')])
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=8008, workers=1)


if __name__ == '__main__':
    main()
