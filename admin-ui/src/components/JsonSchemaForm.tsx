/**
 * Renders a credentials form from a Pydantic-generated JSON Schema.
 *
 * - string                      -> text field (or select when the schema has an `enum`)
 * - `format: password`/writeOnly -> password field, never pre-filled; the masked hint is shown
 * - integer / number            -> number field
 * - boolean                     -> checkbox
 * - object / array / anything else -> JSON text area
 *
 * Secrets left blank are omitted from the payload, which the backend treats as "keep stored value".
 */
import type { JsonSchema } from '../types';

export type FieldKind = 'text' | 'password' | 'integer' | 'number' | 'boolean' | 'select' | 'json';
export type FieldValue = string | boolean;
export type FormValues = Record<string, FieldValue>;

export interface FieldSpec {
  name: string;
  label: string;
  description?: string;
  kind: FieldKind;
  required: boolean;
  nullable: boolean;
  /** For JSON fields: whether the nested schema contains secrets (never pre-filled). */
  containsSecret: boolean;
  options?: string[];
  defaultValue?: unknown;
  minimum?: number;
  maximum?: number;
}

const MAX_DEPTH = 8;

function resolveRef(schema: JsonSchema, root: JsonSchema): JsonSchema {
  if (!schema.$ref) return schema;
  const match = /^#\/(?:\$defs|definitions)\/(.+)$/.exec(schema.$ref);
  const target = match ? root.$defs?.[match[1]] : undefined;
  return target ? { ...target, ...schema, $ref: undefined } : schema;
}

function typesOf(schema: JsonSchema): string[] {
  if (Array.isArray(schema.type)) return schema.type;
  return schema.type ? [schema.type] : [];
}

function isSecret(schema: JsonSchema): boolean {
  return schema.format === 'password' || schema.writeOnly === true;
}

function containsSecret(schema: JsonSchema, root: JsonSchema, depth = 0): boolean {
  if (depth > MAX_DEPTH) return false;
  const s = resolveRef(schema, root);
  if (isSecret(s)) return true;
  const children: JsonSchema[] = [
    ...Object.values(s.properties ?? {}),
    ...(s.items ? [s.items] : []),
    ...(typeof s.additionalProperties === 'object' ? [s.additionalProperties] : []),
    ...(s.anyOf ?? []),
    ...(s.oneOf ?? []),
    ...(s.allOf ?? []),
  ];
  return children.some((child) => containsSecret(child, root, depth + 1));
}

function humanize(name: string): string {
  const words = name.replace(/_/g, ' ').trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function classify(prop: JsonSchema, root: JsonSchema): { kind: FieldKind; nullable: boolean; schema: JsonSchema } {
  let schema = resolveRef(prop, root);
  let nullable = false;

  const variants = schema.anyOf ?? schema.oneOf;
  if (variants) {
    const resolved = variants.map((v) => resolveRef(v, root));
    const nonNull = resolved.filter((v) => !typesOf(v).includes('null') || typesOf(v).length > 1);
    nullable = nonNull.length < resolved.length;
    if (nonNull.length === 1) {
      schema = { ...schema, ...nonNull[0], anyOf: undefined, oneOf: undefined };
    } else {
      const kinds = new Set(nonNull.flatMap(typesOf));
      // Pydantic renders Decimal as anyOf [number, string(pattern)].
      if (kinds.has('number') && [...kinds].every((k) => k === 'number' || k === 'string' || k === 'integer')) {
        return { kind: 'number', nullable, schema };
      }
      return { kind: 'json', nullable, schema };
    }
  }

  const types = typesOf(schema);
  if (types.includes('null')) nullable = true;
  const main = types.filter((t) => t !== 'null');
  const type = main.length === 1 ? main[0] : main.length === 0 && schema.enum ? 'string' : undefined;

  if (isSecret(schema) && (type === 'string' || type === undefined)) return { kind: 'password', nullable, schema };
  if (schema.enum && schema.enum.every((v) => typeof v === 'string')) return { kind: 'select', nullable, schema };
  switch (type) {
    case 'string':
      return { kind: 'text', nullable, schema };
    case 'integer':
      return { kind: 'integer', nullable, schema };
    case 'number':
      return { kind: 'number', nullable, schema };
    case 'boolean':
      return { kind: 'boolean', nullable, schema };
    default:
      return { kind: 'json', nullable, schema };
  }
}

export function fieldsFromSchema(root: JsonSchema | null | undefined): FieldSpec[] {
  if (!root?.properties) return [];
  const required = new Set(root.required ?? []);
  return Object.entries(root.properties).map(([name, prop]) => {
    const { kind, nullable, schema } = classify(prop, root);
    return {
      name,
      label: prop.title ?? schema.title ?? humanize(name),
      description: prop.description ?? schema.description,
      kind,
      required: required.has(name),
      nullable,
      containsSecret: kind === 'json' && containsSecret(prop, root),
      options: kind === 'select' ? (schema.enum as string[]) : undefined,
      defaultValue: prop.default,
      minimum: schema.minimum,
      maximum: schema.maximum,
    };
  });
}

/** Initial form values: non-secret fields pre-filled from the stored hint (or schema default). */
export function initialValues(fields: FieldSpec[], hint: Record<string, unknown> | null | undefined): FormValues {
  const values: FormValues = {};
  for (const field of fields) {
    const stored = hint && field.name in hint ? hint[field.name] : undefined;
    const source = stored !== undefined ? stored : field.defaultValue;
    switch (field.kind) {
      case 'password':
        values[field.name] = '';
        break;
      case 'boolean':
        values[field.name] = typeof source === 'boolean' ? source : false;
        break;
      case 'json':
        values[field.name] =
          field.containsSecret || source === undefined || source === null ? '' : JSON.stringify(source, null, 2);
        break;
      default:
        values[field.name] = source === undefined || source === null ? '' : String(source);
    }
  }
  return values;
}

export interface BuildResult {
  credentials: Record<string, unknown>;
  errors: Record<string, string>;
}

/**
 * Converts form values into the `credentials` payload. `hasStored` tells whether an account with
 * stored credentials already exists (blank required secrets are then allowed: "keep stored").
 */
export function buildCredentials(fields: FieldSpec[], values: FormValues, hasStored: boolean): BuildResult {
  const credentials: Record<string, unknown> = {};
  const errors: Record<string, string> = {};

  for (const field of fields) {
    const raw = values[field.name];
    if (field.kind === 'boolean') {
      credentials[field.name] = raw === true;
      continue;
    }
    const text = typeof raw === 'string' ? raw.trim() : '';
    if (text === '') {
      if (field.kind === 'password' || field.kind === 'json') {
        if (field.required && !hasStored && field.defaultValue === undefined) errors[field.name] = 'Required';
        continue; // omitted -> backend keeps the stored value
      }
      if (field.required && field.defaultValue === undefined) {
        errors[field.name] = 'Required';
      } else if (field.nullable) {
        credentials[field.name] = null;
      }
      continue;
    }
    switch (field.kind) {
      case 'integer': {
        const n = Number(text);
        if (!Number.isInteger(n)) errors[field.name] = 'Enter a whole number';
        else credentials[field.name] = n;
        break;
      }
      case 'number': {
        const n = Number(text);
        if (!Number.isFinite(n)) errors[field.name] = 'Enter a number';
        else credentials[field.name] = text; // keep as string so Decimals stay exact
        break;
      }
      case 'json':
        try {
          credentials[field.name] = JSON.parse(text) as unknown;
        } catch (err) {
          errors[field.name] = `Invalid JSON: ${err instanceof Error ? err.message : String(err)}`;
        }
        break;
      default:
        credentials[field.name] = text;
    }
  }
  return { credentials, errors };
}

function maskedHint(value: unknown): string | null {
  if (typeof value === 'string' && value) return value;
  if (value && typeof value === 'object') return JSON.stringify(value);
  return null;
}

export function JsonSchemaForm({
  fields,
  values,
  errors,
  hint,
  onChange,
  disabled = false,
}: {
  fields: FieldSpec[];
  values: FormValues;
  errors: Record<string, string>;
  hint: Record<string, unknown> | null | undefined;
  onChange: (name: string, value: FieldValue) => void;
  disabled?: boolean;
}) {
  if (fields.length === 0) {
    return <s-paragraph color="subdued">This carrier needs no credentials.</s-paragraph>;
  }

  return (
    <s-stack gap="base">
      {fields.map((field) => {
        const value = values[field.name];
        const text = typeof value === 'string' ? value : '';
        const error = errors[field.name];
        const stored = hint ? hint[field.name] : undefined;

        switch (field.kind) {
          case 'password': {
            const saved = maskedHint(stored);
            const details = [field.description, saved ? `Saved: ${saved} — leave blank to keep` : null]
              .filter(Boolean)
              .join(' · ');
            return (
              <s-password-field
                key={field.name}
                label={field.label}
                name={field.name}
                value={text}
                placeholder={saved ?? undefined}
                details={details || undefined}
                error={error}
                required={field.required && !saved}
                disabled={disabled}
                autocomplete="new-password"
                onInput={(e) => onChange(field.name, e.currentTarget.value)}
              />
            );
          }
          case 'boolean':
            return (
              <s-checkbox
                key={field.name}
                label={field.label}
                name={field.name}
                checked={value === true}
                details={field.description}
                error={error}
                disabled={disabled}
                onChange={(e) => onChange(field.name, e.currentTarget.checked)}
              />
            );
          case 'integer':
          case 'number':
            return (
              <s-number-field
                key={field.name}
                label={field.label}
                name={field.name}
                value={text}
                details={field.description}
                error={error}
                required={field.required}
                disabled={disabled}
                step={field.kind === 'integer' ? 1 : 0.01}
                min={field.minimum}
                max={field.maximum}
                onInput={(e) => onChange(field.name, e.currentTarget.value)}
              />
            );
          case 'select':
            return (
              <s-select
                key={field.name}
                label={field.label}
                name={field.name}
                value={text}
                details={field.description}
                error={error}
                disabled={disabled}
                onChange={(e) => onChange(field.name, e.currentTarget.value)}
              >
                {!field.required && <s-option value="">—</s-option>}
                {(field.options ?? []).map((opt) => (
                  <s-option key={opt} value={opt}>
                    {opt}
                  </s-option>
                ))}
              </s-select>
            );
          case 'json': {
            const saved = field.containsSecret ? maskedHint(stored) : null;
            const details = [
              field.description,
              'JSON value',
              saved ? `Contains secrets. Saved: ${saved} — leave blank to keep` : null,
            ]
              .filter(Boolean)
              .join(' · ');
            return (
              <s-text-area
                key={field.name}
                label={field.label}
                name={field.name}
                value={text}
                rows={4}
                placeholder={field.containsSecret ? '{ }' : undefined}
                details={details}
                error={error}
                disabled={disabled}
                onInput={(e) => onChange(field.name, e.currentTarget.value)}
              />
            );
          }
          default:
            return (
              <s-text-field
                key={field.name}
                label={field.label}
                name={field.name}
                value={text}
                details={field.description}
                error={error}
                required={field.required}
                disabled={disabled}
                autocomplete="off"
                onInput={(e) => onChange(field.name, e.currentTarget.value)}
              />
            );
        }
      })}
    </s-stack>
  );
}
