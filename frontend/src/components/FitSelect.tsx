import type { SelectHTMLAttributes } from 'react'

// A <select> as wide as its selected option, not its longest one: an invisible copy of the selected
// label, with the select's own class (same font, padding and max-width), sizes the cell they share.
export function FitSelect({ label, wrapClassName, className, children, ...props }: SelectHTMLAttributes<HTMLSelectElement> & {
  label: string
  wrapClassName?: string
}) {
  return (
    <span className={`fit-select${wrapClassName ? ` ${wrapClassName}` : ''}`}>
      <span className={`${className ?? ''} fit-select__sizer`} aria-hidden>{label}<span className="fit-select__arrow" /></span>
      <select className={className} {...props}>{children}</select>
    </span>
  )
}
