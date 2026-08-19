"use client";

import { useEffect, useRef, useState } from "react";
import { Check, ChevronDown } from "lucide-react";

import { CompanyLogo } from "@/components/CompanyLogo";

export type SelectOption = { value: string; label: string; companyKey?: string };

export function SelectField({
  label,
  value,
  options,
  onValueChange,
  ariaLabel,
  className = "",
  triggerClassName = "",
}: {
  label?: string;
  value: string;
  options: SelectOption[];
  onValueChange: (value: string) => void;
  ariaLabel: string;
  className?: string;
  triggerClassName?: string;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const selected = options.find((option) => option.value === value);

  useEffect(() => {
    function closeOnOutsideClick(event: MouseEvent) {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    }
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", closeOnOutsideClick);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("mousedown", closeOnOutsideClick);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, []);

  return (
    <div className={`field select-field ${className}`} ref={rootRef}>
      {label && <span className="field-label">{label}</span>}
      <button
        type="button"
        className={`select-trigger ${triggerClassName}`}
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <span>{selected?.label ?? value}</span>
        <ChevronDown size={15} className="select-icon" aria-hidden="true" />
      </button>
      {open && (
        <div className="select-content" role="listbox" aria-label={ariaLabel}>
          <div className="select-viewport">
            {options.map((option) => (
              <button
                type="button"
                role="option"
                aria-selected={option.value === value}
                key={option.value}
                className="select-item"
                onClick={() => {
                  onValueChange(option.value);
                  setOpen(false);
                }}
              >
                <span className="select-option-label">
                  {option.companyKey && <CompanyLogo companyKey={option.companyKey} companyName={option.label} />}
                  <span>{option.label}</span>
                </span>
                {option.value === value && <Check size={14} className="select-check" />}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
