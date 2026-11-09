# Frontend Review Pack

This is the Next.js / React / TypeScript / Tailwind / shadcn review pack for the slopbuster-audit skill.

---

## 0. Core Review Principle

Code review is NOT about:
- Personal preference
- Micro-optimizations
- Clever tricks

Code review IS about:
- Architectural consistency
- Predictable data flow
- Correct abstraction boundaries
- Long-term maintainability

If a change improves short-term speed but harms system clarity, it must be rejected.

---

## 1. High-Level Architecture

### 1.1 Project Structure

Check:
- Is the folder structure aligned with the guideline?
- Are UI / domain / container layers clearly separated?
- Are hooks, services, and types centralized?

Reject if:
- UI logic is scattered
- Domain logic leaks into UI components
- `app/` contains business logic

---

### 1.2 Client / Server Boundary

Check:
- Are server components used for data fetching?
- Are client components explicitly marked?
- Is data serialized across boundaries?

Reject if:
- Client code performs server-only logic
- Server components contain UI state
- Boundary rules are violated for convenience

---

## 2. Component Design

### 2.1 UI Components (`components/ui`)

UI components MUST:
- Be pure and presentational
- Accept only props
- Have explicit typing
- Be reusable across domains

Reject if:
- UI component fetches data
- UI component contains domain logic
- UI component manages complex state

---

### 2.2 Domain Components (`components/domain`)

Domain components MUST:
- Translate domain data to UI props
- Use hooks for logic
- Remain independent of routing

Check:
- Is domain logic centralized?
- Is composition clear and readable?

Reject if:
- Domain logic is duplicated
- Domain components behave like pages

---

### 2.3 Container / Page Components

Page components MUST:
- Orchestrate data only
- Avoid styling details
- Avoid domain logic

Reject if:
- Page contains business rules
- Page contains detailed UI logic

---

## 3. State Model

State is the most common source of long-term rot; review it rigorously.

### 3.1 State Definition

Check:
- Are all non-trivial states explicitly defined?
- Are union types used for multi-state logic?

Reject if:
- Boolean explosion exists
- Implicit state assumptions exist
- Invalid states are representable

---

### 3.2 State Location

Check:
- Is state lifted appropriately?
- Is state duplicated across components?

Reject if:
- The same state exists in multiple places
- State ownership is unclear

---

## 4. Data Flow

Check:
- Is data flow top-down?
- Are events propagated bottom-up?
- Is there any circular dependency?

Reject if:
- A child mutates parent state indirectly
- Global mutable state is abused

---

## 5. Rendering and Lifecycle

### 5.1 Render Purity

Check:
- Is render logic pure?
- Are there side effects during render?

Reject if:
- API calls happen during render
- DOM manipulation occurs in render

---

### 5.2 `useEffect` Usage

`useEffect` MUST:
- Be used only for genuine side effects
- Have complete dependency arrays

Reject if:
- `useEffect` is used to patch over logic errors
- Dependencies are suppressed

---

## 6. TypeScript Discipline

### 6.1 Typing

Check:
- Are all props typed?
- Are API responses typed?
- Is `any` avoided?

Reject if:
- Type assertions are used to silence errors
- Types are bypassed for speed

---

### 6.2 Domain Types

Check:
- Are domain models centralized?
- Are types reused instead of redefined?

Reject if:
- Shape duplication exists
- Types drift across layers

---

## 7. Tailwind and Design System

### 7.1 Tailwind Usage

Check:
- Are design tokens used consistently?
- Are arbitrary values justified?

Reject if:
- Styling is ad-hoc
- Design consistency is broken

---

### 7.2 shadcn Components

Check:
- Are shadcn variants documented?
- Are changes reflected in the Design Page?

Reject if:
- Local overrides bypass the design system
- Component variants diverge silently

---

## 8. Design Page Compliance

This section is mandatory.

Check:
- Does a Design Page exist?
- Are all components represented?
- Are variants visible?

Reject if:
- Components are not showcased
- The Design Page is outdated

No feature is considered complete without Design Page representation.

---

## 9. Performance and Complexity

Check:
- Is memoization justified?
- Is complexity proportional to requirements?

Reject if:
- `useMemo` / `useCallback` is used prematurely
- Complexity is introduced without need

---

## 10. Design & Content Slop

The frontend-specific slop AI generators produce constantly. Catch it.

- **Div soup / non-semantic markup.** Walls of `<div>` where the meaning calls for
  `<button>`, `<a>`, `<nav>`, `<header>`, `<main>`, `<ul>`, headings. The tell: `<div onClick>`
  for things that are actually buttons or links, missing landmarks, no heading hierarchy,
  ARIA bolted on to fake what a semantic element would give for free. This breaks the
  accessibility tree and keyboard nav. Flag interactive `<div>`/`<span>` and missing semantics.
- **Wrapper-div nesting.** Layers of nested divs that exist only to hold a class — collapsible
  with modern CSS (grid/flex, `:has()`, container queries). Each meaningless wrapper is markup slop.
- **Explanatory text written into the UI.** Implementation notes, dev-facing commentary, or
  long paragraphs of how-it-works prose rendered on the page. Frontend copy is product surface,
  not a place to explain the code.
- **Text walls instead of structure.** A blob of prose where users need scannable structure —
  hierarchy, bullets, front-loaded key info. Users scan, not read; unstructured copy is a defect.
- **Wrong audience ("to who").** Content that doesn't match who actually reads it — developer
  jargon on a page meant for end users, or vague marketing fluff where a user needs a concrete
  next step. The same screen for an end user vs. an investor vs. an admin is *different copy*,
  not the same text. Flag any string whose intended reader is unclear or mismatched.

---

## 11. Final Verdict Rules

If any of the following is true, the review MUST FAIL:
- Architecture rules are violated
- State model is unclear
- Design system is bypassed
- Type safety is compromised

Passing code review means: the system is easier to reason about than it was before.
