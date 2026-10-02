import { useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { api } from '../api';
import { ConfirmModal, type ConfirmModalHandle } from '../components/ConfirmModal';
import { EmptyMessage, ErrorBanner, Loading, NoticeBanners } from '../components/Feedback';
import { useApi } from '../hooks/useApi';
import { useNotice } from '../hooks/useNotice';
import type { AllocationStrategy, BalancedWeights, Rule, RuleIn, RuleParams } from '../types';
import { formatDateTime, joinList, parseList, parseOptionalInt, toInput } from '../utils';

const STRATEGIES: { value: AllocationStrategy; label: string; help: string }[] = [
  { value: 'FASTEST', label: 'Fastest', help: 'Lowest estimated delivery days; ties broken by cost.' },
  { value: 'CHEAPEST', label: 'Cheapest', help: 'Lowest quoted shipping cost; ties broken by speed.' },
  { value: 'BALANCED', label: 'Balanced', help: 'Weighted score of speed, cost, performance and carrier priority.' },
  { value: 'PRIORITY', label: 'Priority', help: 'Carrier priority (or the carrier order below), lowest first.' },
  { value: 'CUSTOM', label: 'Custom order', help: 'Explicit carrier preference order (required), optionally restricted.' },
];

const strategyLabel = (s: AllocationStrategy) => STRATEGIES.find((x) => x.value === s)?.label ?? s;

const DEFAULT_WEIGHTS: BalancedWeights = { speed: 0.4, cost: 0.4, performance: 0.1, priority: 0.1 };

function conditionsSummary(conditions: Record<string, unknown>): string {
  if (!conditions || Object.keys(conditions).length === 0) return 'Always';
  const text = JSON.stringify(conditions);
  return text.length > 80 ? `${text.slice(0, 77)}…` : text;
}

const DSL_HELP = `{} matches every order. Leaf: {"field": "payment_mode", "op": "eq", "value": "COD"}.
Combine with {"all": [...]}, {"any": [...]}, {"not": {...}}.
Fields: payment_mode, destination_pincode, destination_state, destination_country (ops: eq, ne, in, not_in, starts_with);
order_value, cod_amount, weight_g, warehouse_id (ops: eq, ne, gt, gte, lt, lte, in, not_in);
skus, tags (ops: contains, contains_any, not_contains). String comparisons are case-insensitive.`;

const CONDITIONS_EXAMPLE = `{
  "all": [
    {"field": "payment_mode", "op": "eq", "value": "COD"},
    {"field": "order_value", "op": "lte", "value": 2000}
  ]
}`;

// --- list --------------------------------------------------------------------------------

export function AllocationRulesPage() {
  const navigate = useNavigate();
  const notice = useNotice();
  const { data, loading, error, reload } = useApi(api.listRules);
  const modal = useRef<ConfirmModalHandle>(null);
  const [pending, setPending] = useState<Rule | null>(null);

  const rules = data ? [...data].sort((a, b) => a.priority - b.priority || a.id - b.id) : null;

  const askDelete = (rule: Rule) => {
    setPending(rule);
    modal.current?.open();
  };

  const remove = async () => {
    if (!pending) return;
    try {
      await api.deleteRule(pending.id);
      notice.showSuccess(`Rule "${pending.name}" deleted`);
      setPending(null);
      await reload();
    } catch (err) {
      notice.showError(err);
    }
  };

  return (
    <s-page heading="Allocation Rules">
      <s-button slot="primary-action" variant="primary" onClick={() => navigate('/allocation-rules/new')}>
        Add rule
      </s-button>
      <s-stack gap="base">
        <s-banner tone="info">
          <s-paragraph>
            Rules are evaluated in priority order (lowest number first); the first active rule whose conditions match
            an order decides how its carrier is chosen. If no rule matches, the <s-text type="strong">FASTEST</s-text>{' '}
            strategy is used.
          </s-paragraph>
        </s-banner>
        <NoticeBanners error={notice.error} success={notice.success} onClear={notice.clear} />
        <ErrorBanner error={error} onRetry={() => void reload()} />
        <s-section>
          {loading && !rules && <Loading label="Loading rules" />}
          {rules && rules.length === 0 && (
            <EmptyMessage heading="No rules yet">Every order uses the FASTEST strategy until you add a rule.</EmptyMessage>
          )}
          {rules && rules.length > 0 && (
            <s-table>
              <s-table-header-row>
                <s-table-header format="numeric" listSlot="kicker">
                  Priority
                </s-table-header>
                <s-table-header listSlot="primary">Name</s-table-header>
                <s-table-header listSlot="inline">Strategy</s-table-header>
                <s-table-header listSlot="inline">Status</s-table-header>
                <s-table-header listSlot="secondary">Conditions</s-table-header>
                <s-table-header>Updated</s-table-header>
                <s-table-header>Actions</s-table-header>
              </s-table-header-row>
              <s-table-body>
                {rules.map((rule) => (
                  <s-table-row key={rule.id}>
                    <s-table-cell>{rule.priority}</s-table-cell>
                    <s-table-cell>
                      <s-link href={`/allocation-rules/${rule.id}`}>{rule.name}</s-link>
                    </s-table-cell>
                    <s-table-cell>
                      <s-badge tone="info">{strategyLabel(rule.strategy)}</s-badge>
                    </s-table-cell>
                    <s-table-cell>
                      {rule.is_active ? <s-badge tone="success">Active</s-badge> : <s-badge tone="neutral">Inactive</s-badge>}
                    </s-table-cell>
                    <s-table-cell>
                      <s-text color="subdued">{conditionsSummary(rule.conditions)}</s-text>
                    </s-table-cell>
                    <s-table-cell>{formatDateTime(rule.updated_at)}</s-table-cell>
                    <s-table-cell>
                      <s-stack direction="inline" gap="small-200">
                        <s-button onClick={() => navigate(`/allocation-rules/${rule.id}`)}>Edit</s-button>
                        <s-button tone="critical" onClick={() => askDelete(rule)}>
                          Delete
                        </s-button>
                      </s-stack>
                    </s-table-cell>
                  </s-table-row>
                ))}
              </s-table-body>
            </s-table>
          )}
        </s-section>
      </s-stack>

      <ConfirmModal
        ref={modal}
        id="delete-rule-modal"
        heading="Delete allocation rule?"
        confirmLabel="Delete rule"
        destructive
        onConfirm={remove}
      >
        <s-paragraph>
          {pending ? `"${pending.name}" will be permanently deleted. This cannot be undone.` : 'Select a rule to delete.'}
        </s-paragraph>
      </ConfirmModal>
    </s-page>
  );
}

// --- form --------------------------------------------------------------------------------

interface RuleForm {
  name: string;
  priority: string;
  is_active: boolean;
  strategy: AllocationStrategy;
  max_cost: string;
  max_edd_days: string;
  min_performance_score: string;
  fallback_carrier: string;
  carrier_order: string;
  only_carriers: string;
  w_speed: string;
  w_cost: string;
  w_performance: string;
  w_priority: string;
  conditions: string;
}

type RuleErrors = Partial<Record<keyof RuleForm, string>>;

function emptyForm(): RuleForm {
  return {
    name: '',
    priority: '100',
    is_active: true,
    strategy: 'FASTEST',
    max_cost: '',
    max_edd_days: '',
    min_performance_score: '',
    fallback_carrier: '',
    carrier_order: '',
    only_carriers: '',
    w_speed: String(DEFAULT_WEIGHTS.speed),
    w_cost: String(DEFAULT_WEIGHTS.cost),
    w_performance: String(DEFAULT_WEIGHTS.performance),
    w_priority: String(DEFAULT_WEIGHTS.priority),
    conditions: '{}',
  };
}

function toForm(rule: Rule): RuleForm {
  const p = rule.params ?? {};
  const w = { ...DEFAULT_WEIGHTS, ...(p.weights ?? {}) };
  return {
    name: rule.name,
    priority: String(rule.priority),
    is_active: rule.is_active,
    strategy: rule.strategy,
    max_cost: toInput(p.max_cost),
    max_edd_days: toInput(p.max_edd_days),
    min_performance_score: toInput(p.min_performance_score),
    fallback_carrier: p.fallback_carrier ?? '',
    carrier_order: joinList(p.carrier_order),
    only_carriers: joinList(p.only_carriers),
    w_speed: String(w.speed),
    w_cost: String(w.cost),
    w_performance: String(w.performance),
    w_priority: String(w.priority),
    conditions: JSON.stringify(rule.conditions ?? {}, null, 2),
  };
}

function buildRule(form: RuleForm, carrierCodes: string[]): { rule: RuleIn | null; errors: RuleErrors } {
  const errors: RuleErrors = {};
  if (!form.name.trim()) errors.name = 'Required';

  const priority = parseOptionalInt(form.priority);
  if (priority === null || Number.isNaN(priority) || priority < 0) errors.priority = 'Enter a whole number ≥ 0';

  let conditions: Record<string, unknown> = {};
  try {
    const parsed: unknown = JSON.parse(form.conditions.trim() || '{}');
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
      errors.conditions = 'Conditions must be a JSON object, e.g. {}';
    } else {
      conditions = parsed as Record<string, unknown>;
    }
  } catch (err) {
    errors.conditions = `Invalid JSON: ${err instanceof Error ? err.message : String(err)}`;
  }

  const params: RuleParams = {};
  const maxCost = form.max_cost.trim();
  if (maxCost) {
    if (!(Number(maxCost) >= 0)) errors.max_cost = 'Enter an amount ≥ 0';
    else params.max_cost = maxCost;
  }
  const edd = parseOptionalInt(form.max_edd_days);
  if (edd !== null) {
    if (Number.isNaN(edd) || edd < 0) errors.max_edd_days = 'Enter a whole number of days ≥ 0';
    else params.max_edd_days = edd;
  }
  const perf = form.min_performance_score.trim();
  if (perf) {
    const n = Number(perf);
    if (!(n >= 0 && n <= 100)) errors.min_performance_score = 'Enter a score between 0 and 100';
    else params.min_performance_score = perf;
  }
  if (form.fallback_carrier) params.fallback_carrier = form.fallback_carrier;

  const checkCodes = (key: 'carrier_order' | 'only_carriers'): string[] => {
    const codes = parseList(form[key]);
    const unknown = codes.filter((c) => !carrierCodes.includes(c));
    if (unknown.length) errors[key] = `Unknown carrier code(s): ${unknown.join(', ')}`;
    else if (new Set(codes).size !== codes.length) errors[key] = 'Each carrier may appear only once';
    return codes;
  };

  if (form.strategy === 'PRIORITY' || form.strategy === 'CUSTOM') {
    const order = checkCodes('carrier_order');
    if (form.strategy === 'CUSTOM' && order.length === 0 && !errors.carrier_order)
      errors.carrier_order = 'CUSTOM needs at least one carrier';
    if (order.length) params.carrier_order = order;
  }
  if (form.strategy === 'CUSTOM') {
    const only = checkCodes('only_carriers');
    if (only.length) params.only_carriers = only;
  }
  if (form.strategy === 'BALANCED') {
    const weight = (key: 'w_speed' | 'w_cost' | 'w_performance' | 'w_priority'): number => {
      const n = Number(form[key].trim() || '0');
      if (!Number.isFinite(n) || n < 0) errors[key] = 'Enter a weight ≥ 0';
      return n;
    };
    params.weights = {
      speed: weight('w_speed'),
      cost: weight('w_cost'),
      performance: weight('w_performance'),
      priority: weight('w_priority'),
    };
  }

  if (Object.keys(errors).length > 0) return { rule: null, errors };
  return {
    rule: {
      name: form.name.trim(),
      priority: priority ?? 100,
      is_active: form.is_active,
      strategy: form.strategy,
      conditions,
      params,
    },
    errors,
  };
}

export function AllocationRuleEditPage() {
  const { id } = useParams();
  const isNew = id === undefined || id === 'new';
  const ruleId = isNew ? null : Number(id);
  const navigate = useNavigate();
  const notice = useNotice();

  const { data, loading, error, reload } = useApi(
    async () => {
      const [rules, carriers] = await Promise.all([ruleId === null ? Promise.resolve([]) : api.listRules(), api.listCarriers()]);
      return { rule: rules.find((r) => r.id === ruleId) ?? null, carriers };
    },
    [ruleId],
  );

  const [form, setForm] = useState<RuleForm>(emptyForm);
  const [errors, setErrors] = useState<RuleErrors>({});
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setForm(data?.rule ? toForm(data.rule) : emptyForm());
    setErrors({});
  }, [data?.rule]);

  const carrierCodes = data?.carriers.map((c) => c.code) ?? [];
  const set = <K extends keyof RuleForm>(key: K, value: RuleForm[K]) => setForm((prev) => ({ ...prev, [key]: value }));

  const save = async () => {
    const { rule, errors: next } = buildRule(form, carrierCodes);
    setErrors(next);
    if (!rule) {
      notice.showError('Please fix the highlighted fields.');
      return;
    }
    setSaving(true);
    try {
      const saved = ruleId === null ? await api.createRule(rule) : await api.updateRule(ruleId, rule);
      notice.showSuccess(`Rule "${saved.name}" saved`);
      navigate('/allocation-rules');
    } catch (err) {
      notice.showError(err);
    } finally {
      setSaving(false);
    }
  };

  const notFound = !isNew && !loading && !error && data !== null && data.rule === null;
  const strategy = STRATEGIES.find((s) => s.value === form.strategy);
  const showOrder = form.strategy === 'PRIORITY' || form.strategy === 'CUSTOM';
  const codesHelp = carrierCodes.length ? `Available: ${carrierCodes.join(', ')}` : '';

  return (
    <s-page heading={isNew ? 'Add allocation rule' : (data?.rule?.name ?? 'Edit allocation rule')}>
      <s-link slot="breadcrumb-actions" href="/allocation-rules">
        Allocation Rules
      </s-link>
      <s-button slot="primary-action" variant="primary" loading={saving} disabled={notFound || !data} onClick={() => void save()}>
        Save
      </s-button>
      <s-button slot="secondary-actions" onClick={() => navigate('/allocation-rules')}>
        Cancel
      </s-button>

      <s-stack gap="base">
        <NoticeBanners error={notice.error} success={notice.success} onClear={notice.clear} />
        <ErrorBanner error={error} onRetry={() => void reload()} />
        {notFound && <ErrorBanner heading="Not found" error="This rule does not exist." />}
        {loading && !data && <Loading label="Loading" />}

        {data && !notFound && (
          <>
            <s-section heading="Rule">
              <s-grid gridTemplateColumns="repeat(auto-fit, minmax(240px, 1fr))" gap="base">
                <s-text-field
                  label="Name"
                  value={form.name}
                  required
                  error={errors.name}
                  onInput={(e) => set('name', e.currentTarget.value)}
                />
                <s-number-field
                  label="Priority"
                  value={form.priority}
                  step={1}
                  min={0}
                  details="Lower = evaluated first"
                  error={errors.priority}
                  onInput={(e) => set('priority', e.currentTarget.value)}
                />
                <s-select
                  label="Strategy"
                  value={form.strategy}
                  details={strategy?.help}
                  onChange={(e) => set('strategy', e.currentTarget.value as AllocationStrategy)}
                >
                  {STRATEGIES.map((s) => (
                    <s-option key={s.value} value={s.value}>
                      {s.label}
                    </s-option>
                  ))}
                </s-select>
              </s-grid>
              <s-box paddingBlockStart="base">
                <s-switch
                  label="Active"
                  checked={form.is_active}
                  onChange={(e) => set('is_active', e.currentTarget.checked)}
                />
              </s-box>
            </s-section>

            <s-section heading="Limits">
              <s-grid gridTemplateColumns="repeat(auto-fit, minmax(200px, 1fr))" gap="base">
                <s-number-field
                  label="Max shipping cost"
                  value={form.max_cost}
                  prefix="₹"
                  min={0}
                  step={0.01}
                  details="Optional"
                  error={errors.max_cost}
                  onInput={(e) => set('max_cost', e.currentTarget.value)}
                />
                <s-number-field
                  label="Max delivery days (EDD)"
                  value={form.max_edd_days}
                  min={0}
                  step={1}
                  details="Optional"
                  error={errors.max_edd_days}
                  onInput={(e) => set('max_edd_days', e.currentTarget.value)}
                />
                <s-number-field
                  label="Min performance score"
                  value={form.min_performance_score}
                  min={0}
                  max={100}
                  step={1}
                  details="Optional, 0–100"
                  error={errors.min_performance_score}
                  onInput={(e) => set('min_performance_score', e.currentTarget.value)}
                />
                <s-select
                  label="Fallback carrier"
                  value={form.fallback_carrier}
                  details="Optional. Used when no carrier meets the limits (hard checks still apply)."
                  onChange={(e) => set('fallback_carrier', e.currentTarget.value)}
                >
                  <s-option value="">None</s-option>
                  {data.carriers.map((c) => (
                    <s-option key={c.code} value={c.code}>
                      {c.display_name} ({c.code})
                    </s-option>
                  ))}
                </s-select>
              </s-grid>
            </s-section>

            {showOrder && (
              <s-section heading="Carrier order">
                <s-stack gap="base">
                  <s-text-field
                    label="Carrier order"
                    value={form.carrier_order}
                    placeholder="e.g. ekart, xpressbees"
                    required={form.strategy === 'CUSTOM'}
                    details={`Comma-separated carrier codes, most preferred first. ${
                      form.strategy === 'PRIORITY' ? 'Overrides carrier priorities. ' : ''
                    }${codesHelp}`}
                    error={errors.carrier_order}
                    onInput={(e) => set('carrier_order', e.currentTarget.value)}
                  />
                  {form.strategy === 'CUSTOM' && (
                    <s-text-field
                      label="Only these carriers"
                      value={form.only_carriers}
                      placeholder="Leave blank to allow all"
                      details={`Optional comma-separated codes restricting the choice. ${codesHelp}`}
                      error={errors.only_carriers}
                      onInput={(e) => set('only_carriers', e.currentTarget.value)}
                    />
                  )}
                </s-stack>
              </s-section>
            )}

            {form.strategy === 'BALANCED' && (
              <s-section heading="Balanced weights">
                <s-paragraph color="subdued">Relative importance of each factor (any non-negative numbers).</s-paragraph>
                <s-grid gridTemplateColumns="repeat(auto-fit, minmax(160px, 1fr))" gap="base">
                  {(
                    [
                      ['w_speed', 'Speed'],
                      ['w_cost', 'Cost'],
                      ['w_performance', 'Performance'],
                      ['w_priority', 'Carrier priority'],
                    ] as const
                  ).map(([key, label]) => (
                    <s-number-field
                      key={key}
                      label={label}
                      value={form[key]}
                      min={0}
                      step={0.05}
                      error={errors[key]}
                      onInput={(e) => set(key, e.currentTarget.value)}
                    />
                  ))}
                </s-grid>
              </s-section>
            )}

            <s-section heading="Conditions">
              <s-stack gap="base">
                <s-text-area
                  label="Conditions (JSON)"
                  value={form.conditions}
                  rows={8}
                  placeholder={CONDITIONS_EXAMPLE}
                  error={errors.conditions}
                  details="Use {} to match every order."
                  onInput={(e) => set('conditions', e.currentTarget.value)}
                />
                <s-box padding="base" background="subdued" borderRadius="base">
                  <s-stack gap="small">
                    <s-text type="strong">Condition language</s-text>
                    {DSL_HELP.split('\n').map((line) => (
                      <s-text key={line} color="subdued">
                        {line}
                      </s-text>
                    ))}
                    <s-stack direction="inline">
                      <s-button variant="tertiary" onClick={() => set('conditions', CONDITIONS_EXAMPLE)}>
                        Insert example (COD orders up to ₹2000)
                      </s-button>
                    </s-stack>
                  </s-stack>
                </s-box>
              </s-stack>
            </s-section>
          </>
        )}
      </s-stack>
    </s-page>
  );
}
