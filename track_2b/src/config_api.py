"""Server-side provider settings; credentials are never returned to the browser."""
import os
import time
from dataclasses import replace
from pathlib import Path
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from providers import generation, Settings, ProviderError

config_router = APIRouter(tags=["模型配置"])
_base_dir = ""
_reload_rag = None


def configure_settings_api(base_dir, reload_rag):
    global _base_dir, _reload_rag
    _base_dir, _reload_rag = base_dir, reload_rag


class ProviderSettingsRequest(BaseModel):
    apertus_base_url: str | None = None
    apertus_api_key: str | None = None
    apertus_model: str | None = None
    apertus_8b_base_url: str | None = None
    apertus_8b_api_key: str | None = None
    apertus_8b_model: str | None = None
    decision_provider: str | None = None
    decision_response_format: str | None = None
    typesafe_api_key: str | None = None
    jev_model: str | None = None
    embedding_base_url: str | None = None
    embedding_api_key: str | None = None
    embedding_model: str | None = None
    embedding_enabled: bool | None = None
    rerank_enabled: bool | None = None
    rerank_base_url: str | None = None
    rerank_api_key: str | None = None
    rerank_model: str | None = None
    rerank_timeout: float | None = None
    apertus_json_mode: bool | None = None


@config_router.post('/api/config/test')
def test_provider_connection():
    try:
        return generation.check_connection()
    except ProviderError as exc:
        return {'status': 'error', 'code': exc.code, 'message': str(exc)}


class DecisionProbeRequest(BaseModel):
    include_generation: bool = False


@config_router.post('/api/config/test-decision')
def test_decision_connection(request: DecisionProbeRequest = DecisionProbeRequest()):
    from decision_contracts import probe
    result = probe()
    if request.include_generation:
        result['generation'] = result.copy() if generation.settings.decision_provider == 'apertus' else probe(model_override='apertus')
    return result


@config_router.post('/api/config/test-retrieval')
def test_retrieval_connection():
    """Bounded probes with generic text; no campaign data or world writes."""
    from openai import OpenAI
    from urllib.parse import urlsplit
    from providers import provider_error
    from rag import complete_vectors
    from reranking import configured, request_scores
    settings=replace(generation.settings)
    results={}
    for kind, enabled, model in (('embedding',settings.embedding_enabled,settings.embedding_model),
                                 ('rerank',settings.rerank_enabled,settings.rerank_model)):
        started=time.perf_counter()
        result={'enabled':enabled,'model':model,'status':'disabled','message':'已关闭'}
        results[kind]=result
        if not enabled: continue
        ready=(bool(settings.embedding_url and (settings.embedding_key or urlsplit(settings.embedding_url).hostname in
               {'localhost','127.0.0.1','::1'})) if kind=='embedding' else configured(settings))
        if not ready:
            result.update(status='unconfigured',message='已开启，需配置服务地址和访问凭据')
            continue
        try:
            if kind=='embedding':
                with OpenAI(base_url=settings.embedding_url,api_key=settings.embedding_key or 'local',
                            timeout=15,max_retries=0) as client:
                    response=client.embeddings.create(model=model,input=['连接检查：钟楼旁有一间图书馆。'])
                vectors=[row.embedding for row in response.data]
                if not complete_vectors(vectors,1): raise ValueError('invalid_vectors')
                result['dimensions']=len(vectors[0])
            else:
                scores,_=request_scores(settings,'图书馆在哪里？',['图书馆在钟楼旁。','海边有一艘船。'])
                if len(scores)!=2: raise ValueError('incomplete_scores')
            result.update(status='ok',message='连接和实际推理均正常')
        except Exception as exc:
            error=provider_error(exc,settings.embedding_url if kind=='embedding' else settings.rerank_base_url)
            result.update(status='error',message=str(error) if isinstance(error,ProviderError) else '服务返回无效结果，请检查地址、模型和凭据')
        result['latency_ms']=round((time.perf_counter()-started)*1000)
    ok=all(r['status']=='ok' for r in results.values())
    return {'status':'success','ready':ok,'services':results}


@config_router.get("/api/config")
@config_router.get("/api/config/keys")
def settings_status():
    s = generation.settings
    from decision_contracts import status as decision_status
    return {"status": "success", "config": {
        "apertus_base_url": s.base_url, "apertus_model": s.model,
        "apertus_8b_base_url": s.small_base_url, "apertus_8b_model": s.small_model,
        "decision_provider": s.decision_provider, "jev_model": s.jev_model,
        "decision_response_format": s.decision_response_format,
        "embedding_base_url": s.embedding_url, "embedding_model": s.embedding_model, "embedding_enabled": s.embedding_enabled,
        "rerank_enabled": s.rerank_enabled, "rerank_base_url": s.rerank_base_url,
        "rerank_model": s.rerank_model, "rerank_timeout": s.rerank_timeout,
        "apertus_json_mode": s.json_mode},
        "keys": {"apertus": {"configured": bool(s.api_key), "required": False},
                 "apertus_8b": {"configured": bool(s.small_api_key), "required": False},
                 "jev": {"configured": bool(s.jev_key), "required": s.decision_provider == "jev"},
                 "embedding": {"configured": bool(s.embedding_key), "required": False},
                 "rerank": {"configured": bool(s.rerank_api_key), "required": False}},
        "features": {"speech": False, "image_generation": False},
        "decision_status": decision_status(s)}


@config_router.post("/api/config")
@config_router.post("/api/config/keys")
def settings_update(req: ProviderSettingsRequest):
    from dotenv import set_key
    updates = req.model_dump(exclude_none=True)
    mapping = {"apertus_base_url": "base_url", "apertus_api_key": "api_key", "apertus_model": "model",
        "apertus_8b_base_url": "small_base_url", "apertus_8b_api_key": "small_api_key", "apertus_8b_model": "small_model",
        "decision_provider": "decision_provider", "typesafe_api_key": "jev_key", "jev_model": "jev_model",
        "decision_response_format": "decision_response_format",
        "embedding_base_url": "embedding_url", "embedding_api_key": "embedding_key", "embedding_model": "embedding_model", "embedding_enabled": "embedding_enabled",
        "rerank_enabled":"rerank_enabled", "rerank_base_url":"rerank_base_url", "rerank_api_key":"rerank_api_key",
        "rerank_model":"rerank_model", "rerank_timeout":"rerank_timeout",
        "apertus_json_mode": "json_mode"}
    if not updates:
        raise HTTPException(422, "没有提供配置")
    for key, value in updates.items():
        if key == 'apertus_base_url' and not value.strip():
            raise HTTPException(422, 'Apertus 服务地址不能为空')
        if isinstance(value, str) and any(c in value for c in "\r\n\0"):
            raise HTTPException(422, "配置不可包含换行")
        if key.endswith("base_url") and value and not value.startswith(("http://", "https://")):
            raise HTTPException(422, "服务地址必须为完整HTTP地址")
        if key in {"apertus_model", "apertus_8b_model", "embedding_model", "rerank_model"} and not value.strip():
            raise HTTPException(422, "模型名称不能为空")
        if key=='rerank_timeout' and not 0.1<=value<=30:
            raise HTTPException(422,'重排超时需在0.1至30秒之间')
    new = replace(generation.settings, **{mapping[k]: v for k, v in updates.items()})
    if new.rerank_enabled:
        from urllib.parse import urlsplit
        try:address=urlsplit(new.rerank_base_url)
        except ValueError as exc:raise HTTPException(422,'重排服务地址格式无效') from exc
        if address.scheme not in ('http','https') or not address.hostname or address.username or address.password or address.query or address.fragment:
            raise HTTPException(422,'启用重排需要独立的完整HTTP服务地址，地址不可含凭据或查询参数')
    if new.decision_provider not in {"apertus", "apertus-8b", "jev", "kev"}:
        raise HTTPException(422, "未知决策模型")
    from decision_contracts import FORMATS
    if new.decision_response_format not in FORMATS:
        raise HTTPException(422, '未知决策输出格式')
    from runtime_config import settings_file, LLM_ALIASES, normalize_layer
    env_file = settings_file(_base_dir)
    env_file.parent.mkdir(parents=True, exist_ok=True)
    aliases = {legacy: standard for standard, legacy in LLM_ALIASES.items()}
    for key, value in updates.items():
        env_key = key.upper()
        text = str(value).lower() if isinstance(value, bool) else str(value)
        set_key(str(env_file), aliases.get(env_key, env_key), text)
        os.environ.update(normalize_layer({env_key: text}))
    if os.name != 'nt':
        env_file.chmod(0o600)
    generation.configure(new)
    if _reload_rag:
        _reload_rag()
    return settings_status()
