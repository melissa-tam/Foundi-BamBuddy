/**
 * Field — shared form primitives for farm-owned UI.
 *
 * `inputClass` is THE single source of the farm text-input/select/textarea
 * styling that was previously copy-pasted byte-for-byte across the SKU,
 * production-run and eject-profile pages (and the first-article banner). The
 * thin `Input`/`Select`/`TextArea` wrappers apply it and merge any extra
 * classes; `FormField` renders the standard label + control + help/error stack
 * with the accessibility wiring (`htmlFor`, `aria-describedby`, `aria-invalid`,
 * `role="alert"`) that every farm form otherwise hand-rolled per field.
 *
 * Deliberately NOT the home for the settings-page input style
 * (`bg-bambu-dark-secondary … rounded-lg`, shared with upstream
 * Email/LDAP settings) nor the compact re-spool modal style — those are
 * different literals, kept local to their components.
 */
import type { ComponentProps, ReactNode } from 'react';

/**
 * THE canonical farm input style. Kept as the exact string the pages
 * previously defined so adopting it changes zero rendered classes.
 */
export const inputClass =
  'w-full px-3 py-2 bg-bambu-dark rounded-md text-white border border-bambu-dark-tertiary ' +
  'focus:outline-none focus:ring-2 focus:ring-bambu-green/50 focus:border-bambu-green transition-colors';

/** Prepend `inputClass`, appending caller classes only when present (so the
 *  no-extra case renders exactly `inputClass` with no trailing space). */
function withInputClass(extra?: string): string {
  return extra ? `${inputClass} ${extra}` : inputClass;
}

/**
 * Native `<input>` with the farm input style applied. Any `className` is merged
 * AFTER `inputClass` so per-field overrides (e.g. an error `border-red-500`)
 * win. All other native input props pass straight through.
 */
export function Input({ className, ...props }: ComponentProps<'input'>) {
  return <input className={withInputClass(className)} {...props} />;
}

/** Native `<select>` with the farm input style applied (see {@link Input}). */
export function Select({ className, ...props }: ComponentProps<'select'>) {
  return <select className={withInputClass(className)} {...props} />;
}

/** Native `<textarea>` with the farm input style applied (see {@link Input}). */
export function TextArea({ className, ...props }: ComponentProps<'textarea'>) {
  return <textarea className={withInputClass(className)} {...props} />;
}

/**
 * Props handed to the control by {@link FormField} via its render-prop child:
 * the field `id` plus the resolved `aria-describedby`/`aria-invalid`. Spread
 * these onto the control (`{...field}`) so it is label-associated and its
 * help/error are announced.
 */
export interface FieldControlProps {
  id: string;
  'aria-describedby': string | undefined;
  'aria-invalid': boolean | undefined;
}

interface FormFieldProps {
  /** Stable id shared by the label (`htmlFor`) and the control. */
  id: string;
  /** Visible label text/content. */
  label: ReactNode;
  /** Optional help text, rendered with id `{id}-help` and linked via
   *  `aria-describedby`. */
  help?: ReactNode;
  /** Optional error text; when present it renders with id `{id}-error`,
   *  `role="alert"`, is linked via `aria-describedby`, and sets
   *  `aria-invalid` on the control. */
  error?: ReactNode;
  /** Appends a subtle required marker to the label. Purely visual — the
   *  control's own `required`/validation stays the caller's responsibility. */
  required?: boolean;
  /** Override the label's classes (defaults to the prominent field style). */
  labelClassName?: string;
  /** Optional classes for the wrapping element. */
  className?: string;
  /** Render-prop returning the control; receives {@link FieldControlProps}. */
  children: (field: FieldControlProps) => ReactNode;
}

const DEFAULT_LABEL_CLASS = 'block text-sm font-medium text-white mb-1';

/**
 * Standard label + control + help/error field. Explicit render-prop (no
 * `cloneElement` magic): the control is `children(fieldProps)` and decides its
 * own type, value and extra props, while `FormField` owns the a11y wiring.
 */
export function FormField({
  id,
  label,
  help,
  error,
  required = false,
  labelClassName = DEFAULT_LABEL_CLASS,
  className,
  children,
}: FormFieldProps) {
  const hasHelp = Boolean(help);
  const hasError = Boolean(error);
  const helpId = hasHelp ? `${id}-help` : undefined;
  const errorId = hasError ? `${id}-error` : undefined;
  const describedBy = [helpId, errorId].filter(Boolean).join(' ') || undefined;

  return (
    <div className={className || undefined}>
      <label htmlFor={id} className={labelClassName}>
        {label}
        {required && (
          <span aria-hidden="true" className="text-red-600 dark:text-red-400">
            {' '}
            *
          </span>
        )}
      </label>
      {children({
        id,
        'aria-describedby': describedBy,
        'aria-invalid': hasError ? true : undefined,
      })}
      {hasHelp && (
        <p id={helpId} className="text-xs text-bambu-gray mt-1">
          {help}
        </p>
      )}
      {hasError && (
        <p id={errorId} role="alert" className="text-red-600 dark:text-red-400 text-xs mt-1">
          {error}
        </p>
      )}
    </div>
  );
}
