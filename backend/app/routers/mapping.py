"""字段映射冲突判定与转换预览接口。

- GET  /api/mapping/options                       解析模板 / 转换器 / 映射表 / 批次选项
- POST /api/mapping/preview                       生成预览（只读，不写正式分发文件）
- GET  /api/mapping/previews/{preview_id}         读取已生成的预览
- POST /api/mapping/previews/{preview_id}/retry   中断后重试并说明原因
- POST /api/mapping/previews/{preview_id}/confirm 确认预览，生成一份转换结果
- GET  /api/mapping/results                       转换结果清单（含历史版本）
- GET  /api/mapping/results/{result_no}           单条转换结果
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.schemas import ActionResult, MappingConfirmRequest, MappingPreviewRequest
from app.services.mapping_preview import (
    MappingActionError,
    MappingLookupError,
    mapping_preview_service,
)

router = APIRouter(prefix="/api/mapping", tags=["字段映射预览"])


@router.get("/options")
def options() -> dict:
    """返回可选的解析模板、转换器、字段映射表、样例批次与判定规则。"""
    return mapping_preview_service.options()


@router.post("/preview")
def create_preview(payload: MappingPreviewRequest) -> dict:
    """生成映射预览；空数据、类型异常和中断都在预览结果里说明，不写正式分发文件。"""
    for label, value in (
        ("解析模板编号", payload.parser_no),
        ("转换器编号", payload.converter_no),
        ("字段映射表编号", payload.mapping_table_id),
        ("样例批次编号", payload.batch_id),
    ):
        if not value.strip():
            raise HTTPException(status_code=400, detail=f"{label}不能为空")
    try:
        return mapping_preview_service.preview(
            payload.parser_no.strip(),
            payload.converter_no.strip(),
            payload.mapping_table_id.strip(),
            payload.batch_id.strip(),
        )
    except MappingLookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/previews/{preview_id}")
def get_preview(preview_id: str) -> dict:
    """读取已有预览；不存在时给出可读原因。"""
    try:
        return mapping_preview_service.get_preview(preview_id)
    except MappingLookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/previews/{preview_id}/retry", response_model=dict)
def retry_preview(preview_id: str) -> dict:
    """重试中断的预览；只有解析中断状态可以重试，恢复原因写在返回里。"""
    try:
        return mapping_preview_service.retry(preview_id)
    except MappingLookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except MappingActionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/previews/{preview_id}/confirm", response_model=ActionResult)
def confirm_preview(preview_id: str, payload: MappingConfirmRequest) -> ActionResult:
    """确认预览后才生成转换结果；重复确认复用同一份结果。"""
    try:
        outcome = mapping_preview_service.confirm(
            preview_id, payload.ack_conflicts, payload.remark
        )
    except MappingLookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except MappingActionError as error:
        return ActionResult(ok=False, message=str(error))
    return ActionResult(
        ok=bool(outcome["ok"]),
        message=str(outcome["message"]),
        entry=outcome["result"],
    )


@router.get("/results")
def list_results() -> dict:
    """转换结果清单：新生成结果与历史版本分列，历史输出格式按当时版本保留。"""
    return mapping_preview_service.results()


@router.get("/results/{result_no}")
def get_result(result_no: str) -> dict:
    """读取单条转换结果；历史版本只读。"""
    result = mapping_preview_service.get_result(result_no)
    if result is None:
        raise HTTPException(status_code=404, detail=f"转换结果 {result_no} 不存在")
    return result
