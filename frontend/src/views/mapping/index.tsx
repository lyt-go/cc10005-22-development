import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const OPTIONS_ENDPOINT = '/api/mapping/options'
const PREVIEW_ENDPOINT = '/api/mapping/preview'
const RESULT_ENDPOINT = '/api/mapping/results'

type OptionBase = { defaults: Record<string, string | number | null> }

type ParserOption = OptionBase & {
  parser_id: number
  parser_no: string
  name: string
  status: string
  paths: { path_id: string; label: string; xpath: string; type_rule: string }[]
}

type ConverterOption = OptionBase & {
  converter_id: number
  converter_no: string
  name: string
  status: string
  input_format: string
  output_format: string
  mapping_table_id: string
}

type TableOption = {
  table_id: string
  name: string
  converter_id: number
  description: string
  row_count: number
  output_fields: string[]
}

type BatchOption = {
  batch_id: string
  label: string
  kind: string
  description: string
  record_count: number
}

type Options = {
  parsers: ParserOption[]
  converters: ConverterOption[]
  mapping_tables: TableOption[]
  batches: BatchOption[]
  field_display_names: Record<string, string>
  priority_rules: string[]
  system_defaults: Record<string, string | number | null>
  guarantees: string[]
}

type ChainItem = {
  source_kind: string
  source_label: string
  row_id: string | null
  parser_path: string | null
  value: string | number | null
  explicit: boolean
  applied: boolean
  reason: string
  force_none?: boolean
}

type Decision = {
  row_id: string
  parser_path: string
  source_field: string
  priority: number
  raw: unknown
  converted: unknown
  decision: string
  reason: string
  conversion_error: string | null
  allow_null_override: boolean
}

type ValueEntry = {
  record_id: string
  value: unknown
  raw: unknown
  winner_row: string
  winner_parser_path: string
  winner_value_kind: string
  decisions: Decision[]
  config: Record<string, unknown>
  config_chains: Record<string, ChainItem[]>
  config_applicability?: Record<string, boolean>
  notes: string[]
  blocking: boolean
  error: string | null
}

type FormatConflict = {
  dimension: string
  label: string
  applicable: boolean
  values: { row_id: string; priority: number; value: unknown }[]
  adopted: string[]
  adopted_sources: string[]
  conclusion: string
}

type FieldEntry = {
  output_field: string
  display_name: string
  type: string
  distribute: boolean
  values: ValueEntry[]
  value_summary: { record_id: string; value: unknown; winner_row: string; winner_parser_path: string; decision: string; blocking: boolean }[]
  winner_sources: string[]
  overridden_sources: string[]
  skipped_sources: string[]
  has_conflict: boolean
  conclusion: string
  format_conflicts: FormatConflict[]
  config_chains: Record<string, Record<string, ChainItem[]>>
  config_applicability?: Record<string, boolean>
  source_errors: { record_id: string; row_id: string; error: string }[]
}

type Conflict = {
  output_field: string
  display_name: string
  parser_paths: string[]
  candidates: {
    row_id: string
    row_order: number
    parser_path: string
    source_field: string
    priority: number
    type: string
    allow_null_override: boolean
    decision_by_record: Record<string, string>
    reason_by_record: Record<string, string>
  }[]
  format_conflicts: FormatConflict[]
  conclusion: string
  severity: string
  blocking: boolean
}

type Preview = {
  preview_id: string
  created_at: string
  state: 'ready' | 'blocked' | 'empty' | 'failed' | 'confirmed'
  state_label: string
  message: string
  failure_reason: string | null
  retryable: boolean
  parser: { parser_no: string; name: string; defaults: Record<string, unknown> }
  converter: {
    converter_no: string
    name: string
    output_format: string
    defaults: Record<string, unknown>
    null_policy: string
  }
  mapping_table: { table_id: string; name: string; row_count: number }
  batch: { batch_id: string; label: string; record_count: number }
  fields: FieldEntry[]
  conflicts: Conflict[]
  records: { record_id: string; values: Record<string, unknown> }[]
  output_preview: {
    output_format: string
    output_fields: string[]
    dropped_fields: string[]
    record_count: number
    content: string
  } | null
  confirm_allowed: boolean
  requires_conflict_ack: boolean
  warnings: string[]
  errors: { record_id: string; output_field: string; error: string }[]
  attempt: { attempt_no: number; previous_state: string | null; resume_reason: string | null }
  confirmed_result_no?: string | null
  rules: string[]
  preview_only: boolean
  writes_distribution: boolean
  preview_only_note: string
}

type ResultItem = {
  result_no: string
  converter_no: string
  parser_no: string
  mapping_table_id: string
  output_version: number
  output_format: string
  record_count: number
  created_at: string
  status: string
  output_content: string
  is_history: boolean
  immutable: boolean
  distribution_note?: string
  spec_snapshot?: Record<string, unknown>
}

function renderValue(value: unknown): string {
  if (value === null || value === undefined) return '∅（空）'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function renderOptionValue(value: unknown): string {
  if (value === null || value === undefined) return '未配置'
  return String(value)
}

function selectedDefaults(record: Record<string, unknown> | undefined): string {
  if (!record) return ''
  return ['date_template', 'precision', 'null_placeholder']
    .map((key) => `${key}=${renderOptionValue(record[key])}`)
    .join('，')
}

export default function MappingPreviewView() {
  const [options, setOptions] = useState<Options | null>(null)
  const [optionsError, setOptionsError] = useState('')
  const [optionsLoading, setOptionsLoading] = useState(true)

  const [parserNo, setParserNo] = useState('')
  const [converterNo, setConverterNo] = useState('')
  const [tableId, setTableId] = useState('')
  const [batchId, setBatchId] = useState('')

  const [preview, setPreview] = useState<Preview | null>(null)
  const [previewError, setPreviewError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [ackConflicts, setAckConflicts] = useState(false)
  const [remark, setRemark] = useState('')

  const [activeField, setActiveField] = useState('')
  const [activeRecord, setActiveRecord] = useState('')

  const [results, setResults] = useState<ResultItem[]>([])
  const [resultsError, setResultsError] = useState('')
  const [activeResult, setActiveResult] = useState<ResultItem | null>(null)

  const loadOptions = useCallback(async () => {
    setOptionsLoading(true)
    setOptionsError('')
    try {
      const response = await request(OPTIONS_ENDPOINT)
      if (!response.ok) throw new Error(`选项读取失败（HTTP ${response.status}）`)
      const payload = (await response.json()) as Options
      setOptions(payload)
      setParserNo((current) => current || payload.parsers[0]?.parser_no || '')
      setConverterNo((current) => current || payload.converters[0]?.converter_no || '')
      setBatchId((current) => current || payload.batches[0]?.batch_id || '')
    } catch (error) {
      setOptionsError(error instanceof Error ? error.message : '选项读取失败')
    } finally {
      setOptionsLoading(false)
    }
  }, [])

  const loadResults = useCallback(async () => {
    setResultsError('')
    try {
      const response = await request(RESULT_ENDPOINT)
      if (!response.ok) throw new Error(`转换结果读取失败（HTTP ${response.status}）`)
      const payload = await response.json()
      setResults(payload.items ?? [])
    } catch (error) {
      setResultsError(error instanceof Error ? error.message : '转换结果读取失败')
    }
  }, [])

  useEffect(() => {
    void loadOptions()
    void loadResults()
  }, [loadOptions, loadResults])

  const converter = useMemo(
    () => options?.converters.find((item) => item.converter_no === converterNo),
    [options, converterNo],
  )
  const availableTables = useMemo(
    () =>
      (options?.mapping_tables ?? []).filter(
        (item) => !converter || item.converter_id === converter.converter_id,
      ),
    [options, converter],
  )
  const parser = useMemo(
    () => options?.parsers.find((item) => item.parser_no === parserNo),
    [options, parserNo],
  )

  useEffect(() => {
    if (!converter) return
    if (availableTables.some((item) => item.table_id === tableId)) return
    setTableId(availableTables[0]?.table_id ?? '')
  }, [converter, availableTables, tableId])

  const resetPreviewState = () => {
    setPreview(null)
    setPreviewError('')
    setAckConflicts(false)
    setNotice('')
    setActiveField('')
    setActiveRecord('')
  }

  async function runPreview() {
    setBusy(true)
    setPreviewError('')
    setNotice('')
    resetPreviewState()
    try {
      const response = await request(PREVIEW_ENDPOINT, {
        method: 'POST',
        body: JSON.stringify({
          parser_no: parserNo,
          converter_no: converterNo,
          mapping_table_id: tableId,
          batch_id: batchId,
        }),
      })
      const payload = await response.json()
      if (!response.ok) {
        throw new Error(payload?.detail ?? `预览生成失败（HTTP ${response.status}）`)
      }
      setPreview(payload as Preview)
      setActiveField(payload.fields?.[0]?.output_field ?? '')
      setActiveRecord(payload.records?.[0]?.record_id ?? '')
    } catch (error) {
      setPreviewError(error instanceof Error ? error.message : '预览生成失败')
    } finally {
      setBusy(false)
    }
  }

  async function retryPreview() {
    if (!preview) return
    setBusy(true)
    setPreviewError('')
    try {
      const response = await request(`/api/mapping/previews/${preview.preview_id}/retry`, {
        method: 'POST',
      })
      const payload = await response.json()
      if (!response.ok) throw new Error(payload?.detail ?? `重试失败（HTTP ${response.status}）`)
      setPreview(payload as Preview)
      setAckConflicts(false)
      setActiveField(payload.fields?.[0]?.output_field ?? '')
      setActiveRecord(payload.records?.[0]?.record_id ?? '')
      setNotice(
        payload.attempt?.resume_reason
          ? `已恢复：${payload.attempt.resume_reason}（第 ${payload.attempt.attempt_no} 次尝试）`
          : '已恢复预览',
      )
    } catch (error) {
      setPreviewError(error instanceof Error ? error.message : '重试失败')
    } finally {
      setBusy(false)
    }
  }

  async function confirmPreview() {
    if (!preview) return
    setBusy(true)
    setPreviewError('')
    setNotice('')
    try {
      const response = await request(`/api/mapping/previews/${preview.preview_id}/confirm`, {
        method: 'POST',
        body: JSON.stringify({ ack_conflicts: ackConflicts, remark: remark || null }),
      })
      const payload = await response.json()
      if (!response.ok) throw new Error(payload?.detail ?? `确认失败（HTTP ${response.status}）`)
      if (!payload.ok) {
        setPreviewError(payload.message ?? '确认被拒绝')
        return
      }
      setNotice(payload.message)
      setPreview((current) =>
        current ? { ...current, confirmed_result_no: payload.entry?.result_no ?? null } : current,
      )
      await loadResults()
    } catch (error) {
      setPreviewError(error instanceof Error ? error.message : '确认失败')
    } finally {
      setBusy(false)
    }
  }

  const field = preview?.fields.find((item) => item.output_field === activeField)
  const recordValues = field?.values.find((item) => item.record_id === activeRecord)

  return (
    <section className="page" data-module="mapping">
      <header className="page-head">
        <div>
          <h2>字段映射与转换预览</h2>
          <p className="page-desc">
            选择解析模板、转换器和字段映射表后生成预览：展示每个输出字段的最终值、被覆盖来源与冲突结论，
            确认后才允许启用转换。预览只读，不写入正式分发文件。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn" type="button" disabled={busy} onClick={() => { void loadOptions(); void loadResults() }}>
            刷新选项与结果
          </button>
        </div>
      </header>

      {optionsLoading && <p className="mapping-hint">正在加载解析模板、转换器与字段映射表…</p>}
      {optionsError && (
        <div className="mapping-error">
          <span className="error-text">{optionsError}</span>
          <button className="btn" type="button" onClick={() => void loadOptions()}>重新加载</button>
        </div>
      )}

      {options && (
        <>
          <form
            className="filter-bar"
            onSubmit={(event) => {
              event.preventDefault()
              void runPreview()
            }}
          >
            <label className="filter-item">
              <span>解析模板</span>
              <select
                value={parserNo}
                onChange={(event) => { setParserNo(event.target.value); resetPreviewState() }}
              >
                {options.parsers.map((item) => (
                  <option key={item.parser_no} value={item.parser_no}>
                    {item.parser_no} · {item.name}（{item.status}）
                  </option>
                ))}
              </select>
            </label>
            <label className="filter-item">
              <span>转换器</span>
              <select
                value={converterNo}
                onChange={(event) => { setConverterNo(event.target.value); resetPreviewState() }}
              >
                {options.converters.map((item) => (
                  <option key={item.converter_no} value={item.converter_no}>
                    {item.converter_no} · {item.input_format}→{item.output_format}（{item.status}）
                  </option>
                ))}
              </select>
            </label>
            <label className="filter-item">
              <span>字段映射表</span>
              <select
                value={tableId}
                onChange={(event) => { setTableId(event.target.value); resetPreviewState() }}
              >
                {availableTables.map((item) => (
                  <option key={item.table_id} value={item.table_id}>
                    {item.table_id} · {item.name}（{item.row_count} 行）
                  </option>
                ))}
                {availableTables.length === 0 && <option value="">该转换器没有登记的字段映射表</option>}
              </select>
            </label>
            <label className="filter-item">
              <span>样例批次</span>
              <select
                value={batchId}
                onChange={(event) => { setBatchId(event.target.value); resetPreviewState() }}
              >
                {options.batches.map((item) => (
                  <option key={item.batch_id} value={item.batch_id}>
                    {item.label}（{item.record_count} 条）
                  </option>
                ))}
              </select>
            </label>
            <button className="btn primary" type="submit" disabled={busy || !tableId}>
              {busy ? '预览中…' : '生成映射预览'}
            </button>
          </form>

          <div className="mapping-meta">
            <span>解析模板默认：{selectedDefaults(parser?.defaults)}</span>
            <span>转换器默认：{selectedDefaults(converter?.defaults)}</span>
            <span>系统默认：{selectedDefaults(options.system_defaults)}</span>
          </div>

          <details className="mapping-rules">
            <summary>优先顺序与保留策略（{options.priority_rules.length} 条）</summary>
            <ul>
              {options.priority_rules.map((rule) => <li key={rule}>{rule}</li>)}
            </ul>
            <ul>
              {options.guarantees.map((rule) => <li key={rule}>{rule}</li>)}
            </ul>
          </details>
        </>
      )}

      {previewError && (
        <div className="mapping-error">
          <span className="error-text">{previewError}</span>
          {preview?.state === 'failed' && (
            <button className="btn" type="button" disabled={busy} onClick={() => void retryPreview()}>
              重试预览
            </button>
          )}
        </div>
      )}
      {notice && <p className="mapping-notice">{notice}</p>}

      {!preview && !previewError && !optionsLoading && (
        <div className="mapping-empty">还没有生成映射预览。选择上方的模板、转换器和映射表后点击“生成映射预览”。</div>
      )}

      {preview && (
        <>
          <section className="mapping-summary">
            <div>
              <strong>{preview.preview_id}</strong>
              <span className={`mapping-badge state-${preview.state}`}>{preview.state_label}</span>
              {preview.confirmed_result_no && (
                <span className="mapping-badge state-confirmed">已生成 {preview.confirmed_result_no}</span>
              )}
            </div>
            <p>{preview.message}</p>
            <p className="mapping-hint">
              {preview.parser.parser_no} × {preview.converter.converter_no} × {preview.mapping_table.table_id}
              {' '}· 批次 {preview.batch.label}（{preview.batch.record_count} 条）
              {' '}· 输出格式 {preview.converter.output_format}
              {' '}· 第 {preview.attempt.attempt_no} 次尝试
            </p>
            {preview.attempt.resume_reason && (
              <p className="mapping-resume">恢复原因：{preview.attempt.resume_reason}</p>
            )}
            {preview.failure_reason && (
              <p className="error-text">中断原因：{preview.failure_reason}</p>
            )}
            <p className="mapping-guarantee">{preview.preview_only_note}</p>
          </section>

          {preview.state === 'failed' && (
            <div className="mapping-empty">
              <p>预览在解析批次时中断，尚未生成任何字段判定。可以重试，重试会继续使用同一批次数据。</p>
              <button className="btn primary" type="button" disabled={busy} onClick={() => void retryPreview()}>
                重试预览
              </button>
            </div>
          )}

          {preview.state === 'empty' && (
            <div className="mapping-empty">
              <p>暂无结果：该批次没有解析出任何记录，因此没有输出字段可预览，也不会生成转换结果。</p>
              <p className="mapping-hint">可以更换样例批次，或先补齐采集结果后重新预览。</p>
            </div>
          )}

          {preview.errors.length > 0 && (
            <div className="mapping-error-block">
              <strong>类型转换阻断（{preview.errors.length} 项，修正后才能确认）</strong>
              <ul>
                {preview.errors.map((item) => (
                  <li key={`${item.record_id}-${item.output_field}-${item.error}`}>
                    {item.record_id} · {item.output_field}：{item.error}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {preview.warnings.length > 0 && (
            <div className="mapping-warn-block">
              <strong>提示（{preview.warnings.length} 项）</strong>
              <ul>
                {preview.warnings.map((item) => <li key={item}>{item}</li>)}
              </ul>
            </div>
          )}

          {preview.fields.length > 0 && (
            <>
              <h3 className="mapping-section-title">字段判定与冲突列表</h3>
              <table className="data-table mapping-field-table">
                <thead>
                  <tr>
                    <th>输出字段</th>
                    <th>类型</th>
                    <th>最终字段值（逐记录）</th>
                    <th>采用来源</th>
                    <th>被覆盖 / 跳过来源</th>
                    <th>冲突结论</th>
                    <th>分发</th>
                    <th>明细</th>
                  </tr>
                </thead>
                <tbody>
                  {preview.fields.map((item) => (
                    <tr
                      key={item.output_field}
                      className={activeField === item.output_field ? 'mapping-row-active' : ''}
                    >
                      <td>
                        <strong>{item.output_field}</strong>
                        <div className="mapping-hint">{item.display_name}</div>
                      </td>
                      <td>{item.type}</td>
                      <td>
                        {item.value_summary.map((summary) => (
                          <div key={summary.record_id} className={summary.blocking ? 'error-text' : ''}>
                            <span className="mapping-hint">{summary.record_id}</span>{' '}
                            {summary.blocking ? '（转换失败）' : renderValue(summary.value)}
                            {summary.decision === '空值覆盖' && (
                              <span className="mapping-tag warn">空值覆盖</span>
                            )}
                          </div>
                        ))}
                      </td>
                      <td>{item.winner_sources.join('、') || '—'}</td>
                      <td>
                        {item.overridden_sources.length > 0 && (
                          <div>覆盖：{item.overridden_sources.join('、')}</div>
                        )}
                        {item.skipped_sources.length > 0 && (
                          <div>空值跳过：{item.skipped_sources.join('、')}</div>
                        )}
                        {item.overridden_sources.length === 0 && item.skipped_sources.length === 0 && '—'}
                      </td>
                      <td className={item.has_conflict ? 'mapping-conclusion' : ''}>{item.conclusion}</td>
                      <td>{item.distribute ? '写入' : '不分发'}</td>
                      <td>
                        <button
                          className="link"
                          type="button"
                          onClick={() => { setActiveField(item.output_field); setActiveRecord(item.values[0]?.record_id ?? '') }}
                        >
                          查看覆盖顺序
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>

              {preview.conflicts.length > 0 && (
                <>
                  <h3 className="mapping-section-title">
                    同一输出字段来自多条解析路径的冲突列表（{preview.conflicts.length} 项）
                  </h3>
                  <div className="mapping-conflict-list">
                    {preview.conflicts.map((conflict) => (
                      <article key={conflict.output_field} className={`mapping-conflict ${conflict.blocking ? 'blocking' : ''}`}>
                        <header>
                          <strong>{conflict.output_field}</strong>（{conflict.display_name}）
                          <span className={`mapping-tag ${conflict.blocking ? 'danger' : 'warn'}`}>
                            {conflict.blocking ? '阻断' : '冲突'}
                          </span>
                          <span className="mapping-hint">解析路径：{conflict.parser_paths.join(' / ')}</span>
                        </header>
                        <table className="data-table">
                          <thead>
                            <tr>
                              <th>映射行</th>
                              <th>解析路径</th>
                              <th>源字段</th>
                              <th>优先级</th>
                              <th>逐记录裁决</th>
                              <th>原因</th>
                            </tr>
                          </thead>
                          <tbody>
                            {conflict.candidates.map((candidate) => (
                              <tr key={candidate.row_id}>
                                <td>{candidate.row_id}</td>
                                <td>{candidate.parser_path}</td>
                                <td>{candidate.source_field}</td>
                                <td>
                                  {candidate.priority}
                                  {candidate.allow_null_override && <span className="mapping-tag warn">允许空值覆盖</span>}
                                </td>
                                <td>
                                  {Object.entries(candidate.decision_by_record).map(([recordId, decision]) => (
                                    <div key={recordId}>
                                      <span className="mapping-hint">{recordId}</span> {decision}
                                    </div>
                                  ))}
                                </td>
                                <td>
                                  {Object.entries(candidate.reason_by_record)
                                    .map(([recordId, reason]) => `${recordId}：${reason}`)
                                    .join('；')}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                        <p className="mapping-conclusion">{conflict.conclusion}</p>
                        <div className="mapping-format-conflicts">
                          {conflict.format_conflicts.map((formatConflict) => (
                            <div key={formatConflict.dimension} className={formatConflict.applicable ? '' : 'mapping-muted'}>
                              <strong>{formatConflict.label}：</strong>
                              {formatConflict.conclusion}
                            </div>
                          ))}
                        </div>
                      </article>
                    ))}
                  </div>
                </>
              )}

              {field && (
                <>
                  <h3 className="mapping-section-title">逐字段覆盖顺序：{field.output_field}（{field.display_name}）</h3>
                  <div className="mapping-record-tabs">
                    {field.values.map((value) => (
                      <button
                        key={value.record_id}
                        type="button"
                        className={activeRecord === value.record_id ? 'btn primary' : 'btn'}
                        onClick={() => setActiveRecord(value.record_id)}
                      >
                        {value.record_id}
                      </button>
                    ))}
                  </div>

                  {recordValues && (
                    <>
                      <p className="mapping-hint">
                        最终值：<strong>{renderValue(recordValues.value)}</strong>
                        {' '}· 原始值：{renderValue(recordValues.raw)}
                        {' '}· 采用映射行：{recordValues.winner_row}（路径 {recordValues.winner_parser_path}，
                        {recordValues.winner_value_kind}）
                      </p>
                      {recordValues.notes.length > 0 && (
                        <ul className="mapping-notes">
                          {recordValues.notes.map((note) => <li key={note}>{note}</li>)}
                        </ul>
                      )}

                      <table className="data-table">
                        <thead>
                          <tr>
                            <th>映射行</th>
                            <th>解析路径</th>
                            <th>源字段</th>
                            <th>优先级</th>
                            <th>原始值</th>
                            <th>转换后</th>
                            <th>裁决</th>
                            <th>原因</th>
                          </tr>
                        </thead>
                        <tbody>
                          {recordValues.decisions.map((decision) => (
                            <tr key={decision.row_id}>
                              <td>{decision.row_id}</td>
                              <td>{decision.parser_path}</td>
                              <td>{decision.source_field}</td>
                              <td>{decision.priority}</td>
                              <td>{renderValue(decision.raw)}</td>
                              <td className={decision.conversion_error ? 'error-text' : ''}>
                                {decision.conversion_error ?? renderValue(decision.converted)}
                              </td>
                              <td>{decision.decision}</td>
                              <td>{decision.reason}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>

                      <h4 className="mapping-section-title">日期模板 / 数值精度 / 空值占位符的来源链</h4>
                      <div className="mapping-chain-grid">
                        {(['date_template', 'precision', 'null_placeholder'] as const).map((dimension) => {
                          const chain = recordValues.config_chains[dimension] ?? []
                          const label = dimension === 'date_template'
                            ? '日期模板'
                            : dimension === 'precision' ? '数值精度' : '空值占位符'
                          const applicable = recordValues.config_applicability?.[dimension] ?? true
                          return (
                            <div key={dimension} className={`mapping-chain ${applicable ? '' : 'mapping-muted'}`}>
                              <header>
                                <strong>{label}</strong>
                                {!applicable && <span className="mapping-tag">本字段不适用</span>}
                              </header>
                              <ol>
                                {chain.map((item, index) => (
                                  <li key={`${item.source_kind}-${item.row_id ?? index}`} className={item.applied ? 'mapping-chain-applied' : ''}>
                                    <span>{item.source_label}</span>
                                    <span className="mapping-hint">值：{renderOptionValue(item.value)}</span>
                                    <span className="mapping-hint">{item.reason}</span>
                                  </li>
                                ))}
                              </ol>
                            </div>
                          )
                        })}
                      </div>
                      <p className="mapping-hint">
                        空值占位符只填充最终值，不参与字段重命名；字段名固定为 {field.output_field}。
                      </p>
                    </>
                  )}
                </>
              )}

              <h3 className="mapping-section-title">逐记录最终字段值</h3>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>记录</th>
                    {preview.output_preview?.output_fields.map((name) => <th key={name}>{name}</th>)}
                  </tr>
                </thead>
                <tbody>
                  {preview.records.map((record) => (
                    <tr key={record.record_id}>
                      <td>{record.record_id}</td>
                      {preview.output_preview?.output_fields.map((name) => (
                        <td key={name}>{renderValue(record.values[name])}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>

              {preview.output_preview && (
                <>
                  <h3 className="mapping-section-title">转换预览（不写入正式分发文件）</h3>
                  <p className="mapping-hint">
                    输出格式 {preview.output_preview.output_format} ·
                    字段 {preview.output_preview.output_fields.length} 个 ·
                    记录 {preview.output_preview.record_count} 条
                    {preview.output_preview.dropped_fields.length > 0 && (
                      <> · 标记不分发：{preview.output_preview.dropped_fields.join('、')}</>
                    )}
                  </p>
                  <pre className="mapping-code">{preview.output_preview.content}</pre>
                </>
              )}

              <div className="mapping-confirm">
                {preview.requires_conflict_ack && (
                  <label className="mapping-ack">
                    <input
                      type="checkbox"
                      checked={ackConflicts}
                      onChange={(event) => setAckConflicts(event.target.checked)}
                    />
                    我已核对上述 {preview.conflicts.length} 项字段冲突与覆盖来源
                  </label>
                )}
                <input
                  className="mapping-remark"
                  value={remark}
                  placeholder="确认备注（可选）"
                  onChange={(event) => setRemark(event.target.value)}
                />
                <button
                  className="btn primary"
                  type="button"
                  disabled={
                    busy ||
                    !preview.confirm_allowed ||
                    Boolean(preview.confirmed_result_no) ||
                    (preview.requires_conflict_ack && !ackConflicts)
                  }
                  onClick={() => void confirmPreview()}
                >
                  {preview.confirmed_result_no ? '已确认，复用已有结果' : '确认并生成转换结果'}
                </button>
                <span className="mapping-hint">
                  确认后只生成一份转换结果草稿，不会写入正式分发文件；重复确认复用同一份结果。
                </span>
              </div>
            </>
          )}
        </>
      )}

      <h3 className="mapping-section-title">转换结果与历史版本</h3>
      {resultsError && (
        <div className="mapping-error">
          <span className="error-text">{resultsError}</span>
          <button className="btn" type="button" onClick={() => void loadResults()}>重试读取</button>
        </div>
      )}
      {!resultsError && results.length === 0 && (
        <div className="mapping-empty">暂无转换结果；确认映射预览后会在这里出现一份转换草稿。</div>
      )}
      {results.length > 0 && (
        <table className="data-table">
          <thead>
            <tr>
              <th>结果编号</th>
              <th>转换器</th>
              <th>解析模板</th>
              <th>映射表</th>
              <th>版本</th>
              <th>输出格式</th>
              <th>记录数</th>
              <th>生成时间</th>
              <th>状态</th>
              <th>内容</th>
            </tr>
          </thead>
          <tbody>
            {results.map((item) => (
              <tr key={item.result_no}>
                <td>{item.result_no}</td>
                <td>{item.converter_no}</td>
                <td>{item.parser_no}</td>
                <td>{item.mapping_table_id}</td>
                <td>v{item.output_version}</td>
                <td>{item.output_format}</td>
                <td>{item.record_count}</td>
                <td>{item.created_at}</td>
                <td>
                  {item.status}
                  {item.is_history && <span className="mapping-tag">历史只读</span>}
                </td>
                <td>
                  <button className="link" type="button" onClick={() => setActiveResult(item)}>查看</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {activeResult && (
        <div className="mapping-result-detail">
          <header>
            <strong>{activeResult.result_no}</strong>
            <span className="mapping-hint">
              v{activeResult.output_version} · {activeResult.output_format} · {activeResult.status}
            </span>
            <button className="link" type="button" onClick={() => setActiveResult(null)}>收起</button>
          </header>
          {activeResult.distribution_note && <p className="mapping-hint">{activeResult.distribution_note}</p>}
          {activeResult.is_history && (
            <p className="mapping-hint">
              历史输出版本按生成时的格式固化，后续转换器配置变化不会改写这份结果。
            </p>
          )}
          <pre className="mapping-code">{activeResult.output_content}</pre>
        </div>
      )}
    </section>
  )
}
