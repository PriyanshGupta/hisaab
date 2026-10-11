// A labelled form field. The <label> holds only the field's name; help text and
// errors sit beside it and are linked with aria-describedby, so assistive tech
// announces "Last 4 digits" as the name and the help as the description.
import { type InputHTMLAttributes, useId } from "react";

type FieldProps = Omit<InputHTMLAttributes<HTMLInputElement>, "id" | "className"> & {
  label: string;
  help?: string | undefined;
  error?: string | undefined;
};

export function Field({ label, help, error, ...input }: FieldProps) {
  const id = useId();
  const helpId = help ? `${id}-help` : undefined;
  const errorId = error ? `${id}-error` : undefined;
  const describedBy = [helpId, errorId].filter(Boolean).join(" ") || undefined;
  return (
    <div className="field">
      <label className="field__label" htmlFor={id}>
        {label}
      </label>
      <input
        {...input}
        id={id}
        className="field__input"
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy}
      />
      {help && (
        <span className="field__help" id={helpId}>
          {help}
        </span>
      )}
      {error && (
        <span className="field__error" id={errorId}>
          {error}
        </span>
      )}
    </div>
  );
}
