<template>
  <section class="jurisdiction-panel" data-testid="jurisdiction-panel">
    <header class="panel-head">
      <div>
        <h3>县级归属与对账</h3>
        <p class="page-desc">
          距离容差内的候选与命中格内的分段分开复核；只有命中格内才有县级确认口径。
        </p>
      </div>
      <div class="panel-actions">
        <input v-model="batchKey" class="batch-input" placeholder="批次编号" data-testid="jc-batch" />
        <input
          v-model.number="tolerance"
          class="tolerance-input"
          type="number"
          min="1"
          step="100"
          placeholder="距离容差(米)"
          data-testid="jc-tolerance"
        />
        <button class="btn primary" type="button" :disabled="busy || !batchKey" @click="recompute">
          调整容差重算
        </button>
        <button class="btn" type="button" :disabled="busy || !batchKey" @click="loadAll">
          刷新归属明细
        </button>
      </div>
    </header>

    <p v-if="message" class="panel-message">{{ message }}</p>
    <p v-if="error" class="error-text">{{ error }}</p>

    <div class="analysis-summary">
      <article v-for="card in summaryCards" :key="card.label" class="analysis-card">
        <span class="stat-label">{{ card.label }}</span>
        <strong class="stat-value">{{ card.value }}</strong>
      </article>
    </div>

    <h4>待复核明细（无法确认归属的分段）</h4>
    <table class="data-table">
      <thead>
        <tr>
          <th>分段</th><th>趟次</th><th>候选网格</th><th>候选县</th>
          <th>原确认县</th><th>候选距离</th><th>状态</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in reviewItems" :key="item.review_id" :data-testid="`jc-review-${item.review_id}`">
          <td>{{ item.segment_id }}</td>
          <td>{{ item.trip_id }}</td>
          <td>{{ item.grid_geohash ?? '—' }}</td>
          <td>{{ item.candidate_county_code ?? '—' }}</td>
          <td>{{ item.confirmed_county_code ?? '—' }}</td>
          <td>{{ candidateDistances(item) }}</td>
          <td>{{ item.status }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="attachEvidence(item)">补齐证据</button>
            <button class="link" type="button" @click="adjust(item)">发起调账</button>
          </td>
        </tr>
        <tr v-if="!reviewItems.length">
          <td colspan="8" class="empty-state">当前批次没有待复核明细</td>
        </tr>
      </tbody>
    </table>

    <h4>下期县报核算（仅未提交）</h4>
    <div class="panel-actions">
      <select v-model="disposition" class="batch-input" data-testid="jc-disposition">
        <option value="continue_confirmed">继续纳入原确认县</option>
        <option value="hold_under_confirmation">本期暂挂，原确认名下保留出处</option>
        <option value="await_adjustment">待新证据后调账</option>
      </select>
      <button class="btn" type="button" :disabled="busy || !batchKey" @click="computeReport">
        核算县报
      </button>
    </div>
    <table class="data-table">
      <thead>
        <tr>
          <th>县</th><th>已确权</th><th>暂挂</th><th>待新证据</th>
          <th>调入</th><th>移出</th><th>已入账</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in reportRows" :key="row.county_code">
          <td>{{ row.county_code }}</td>
          <td>{{ row.confirmed_trips }}</td>
          <td>{{ row.held_trips }}</td>
          <td>{{ row.pending_evidence_trips }}</td>
          <td>{{ row.adjusted_in_trips }}</td>
          <td>{{ row.adjusted_out_trips }}</td>
          <td>{{ row.booked_trips }}</td>
        </tr>
        <tr v-if="!reportRows.length">
          <td colspan="7" class="empty-state">尚无县报核算结果</td>
        </tr>
      </tbody>
    </table>
    <p v-if="report" class="page-foot">
      <span>上级统计合计 {{ report.total_trips }} 趟，已入账 {{ report.total_booked }} 趟</span>
      <span v-if="report.double_booked" class="error-text">检测到同一趟被重复入账</span>
    </p>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request, fetchJson } from '@/api/client'

type ReviewItem = {
  review_id: string
  segment_id: string
  trip_id: string
  grid_geohash: string | null
  candidate_county_code: string | null
  confirmed_county_code: string | null
  status: string
  candidates: { grid_geohash: string; distance: number }[]
}

type CountyRow = {
  county_code: string
  confirmed_trips: number
  held_trips: number
  pending_evidence_trips: number
  adjusted_in_trips: number
  adjusted_out_trips: number
  booked_trips: number
}

type CountyReport = {
  counties: CountyRow[]
  total_trips: number
  total_booked: number
  double_booked: boolean
}

type JoinSummary = {
  total_segments: number
  hit_in_grid: number
  distance_only: number
  county_confirmed: number
}

const props = defineProps<{ moduleKey: string }>()
const batchKey = ref('')
const tolerance = ref(500)
const disposition = ref('hold_under_confirmation')
const reviewItems = ref<ReviewItem[]>([])
const report = ref<CountyReport | null>(null)
const summary = ref<JoinSummary | null>(null)
const busy = ref(false)
const message = ref('')
const error = ref('')

const reportRows = computed(() => report.value?.counties ?? [])
const summaryCards = computed(() => [
  { label: '关联分段', value: summary.value?.total_segments ?? 0 },
  { label: '命中格内', value: summary.value?.hit_in_grid ?? 0 },
  { label: '仅距离内候选', value: summary.value?.distance_only ?? 0 },
  { label: '待复核明细', value: reviewItems.value.filter((item) => item.status === '待复核').length },
])

function candidateDistances(item: ReviewItem) {
  if (!item.candidates?.length) return '—'
  return item.candidates
    .map((candidate) => `${candidate.grid_geohash}:${candidate.distance}m`)
    .join('，')
}

async function post(path: string, body: Record<string, unknown>) {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  return (await response.json()) as Record<string, unknown>
}

async function loadAll() {
  if (!batchKey.value) return
  busy.value = true
  error.value = ''
  message.value = ''
  try {
    const query = `?batch_key=${encodeURIComponent(batchKey.value)}`
    const detail = await fetchJson<{ items: ReviewItem[]; county_report: CountyReport | null }>(
      `/api/jurisdiction/review-items${query}`,
    )
    reviewItems.value = detail.items
    report.value = detail.county_report
    summary.value = await fetchJson<JoinSummary>(`/api/spatial_join/summary${query}`)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '归属明细读取失败'
  } finally {
    busy.value = false
  }
}

async function recompute() {
  busy.value = true
  error.value = ''
  message.value = ''
  try {
    const result = await post('/api/spatial_join/recompute', {
      batch_key: batchKey.value,
      distance_tolerance: tolerance.value,
    })
    message.value = `重算完成：命中格内 ${result.hit_in_grid} 条，仅距离内 ${result.distance_only} 条`
    await loadAll()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '重算失败'
  } finally {
    busy.value = false
  }
}

async function attachEvidence(item: ReviewItem) {
  busy.value = true
  error.value = ''
  message.value = ''
  try {
    const result = await post('/api/jurisdiction/review-items/evidence', {
      batch_key: batchKey.value,
      review_id: item.review_id,
      evidence_ref: `WEB-${item.review_id}`,
      geometry_evidence: { boundary_overlap_ratio: 0.9, crosses_boundary: true },
    })
    message.value = String(result.message ?? '')
    await loadAll()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '证据补交失败'
  } finally {
    busy.value = false
  }
}

async function adjust(item: ReviewItem) {
  const target = item.candidate_county_code
  if (!target) {
    error.value = '该明细还没有可转入的候选县，请先补齐边界证据'
    return
  }
  busy.value = true
  error.value = ''
  message.value = ''
  try {
    const result = await post('/api/jurisdiction/adjustments', {
      batch_key: batchKey.value,
      review_id: item.review_id,
      to_county_code: target,
      evidence_ref: item.review_id ? `WEB-${item.review_id}` : '',
    })
    message.value = String(result.message ?? '')
    await loadAll()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '调账失败'
  } finally {
    busy.value = false
  }
}

async function computeReport() {
  busy.value = true
  error.value = ''
  message.value = ''
  try {
    const result = await post('/api/jurisdiction/county-report', {
      batch_key: batchKey.value,
      disposition: disposition.value,
      submitted: false,
    })
    message.value = String(result.message ?? '')
    const payload = result.report as CountyReport | undefined
    if (payload) report.value = payload
  } catch (err) {
    error.value = err instanceof Error ? err.message : '县报核算失败'
  } finally {
    busy.value = false
  }
}

onMounted(() => {
  if (props.moduleKey) void loadAll()
})
</script>

<style scoped>
.jurisdiction-panel {
  margin-top: 20px;
  padding: 16px;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
}
.panel-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; flex-wrap: wrap; }
.panel-head h3 { margin: 0; font-size: 15px; }
.panel-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin: 10px 0; }
.batch-input, .tolerance-input {
  padding: 6px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: 13px;
}
.tolerance-input { width: 140px; }
.panel-message { font-size: 13px; color: #1f6feb; }
h4 { font-size: 13px; margin: 16px 0 8px; }
</style>
