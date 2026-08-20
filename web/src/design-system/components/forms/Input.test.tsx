import { describe, expect, it, vi } from 'vitest'
import { renderComponent, screen } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { Input } from './index'

/** Every id in `aria-describedby` must resolve, or the description is announced to nobody. */
function describedBy(el: HTMLElement): HTMLElement[] {
  const ids = (el.getAttribute('aria-describedby') ?? '').split(/\s+/).filter(Boolean)
  return ids.map((id) => {
    const node = el.ownerDocument.getElementById(id)
    if (!node) throw new Error(`aria-describedby points at "${id}", which is not in the document`)
    return node
  })
}

/** patterns.md §13 — a credential is write-only. Nothing may echo it back into the markup. */
function expectValueNeverEchoed(container: HTMLElement, control: HTMLElement, secret: string) {
  expect(screen.queryByText(secret)).toBeNull()
  for (const el of container.querySelectorAll('*')) {
    if (el === control) continue
    for (const attr of el.attributes) {
      expect(attr.value).not.toContain(secret)
    }
  }
}

describe('Input', () => {
  describe('contract', () => {
    it('renders the class the CSS expects and binds the label to the control', () => {
      renderComponent(<Input id="base_url" label="Base URL" />)

      const field = screen.getByLabelText('Base URL')
      expect(field).toHaveClass('u-input')
      expect(field.tagName).toBe('INPUT')
    })

    it('renders the mono face for identifiers', () => {
      renderComponent(<Input id="device_id" label="Device id" mono />)

      expect(screen.getByLabelText('Device id')).toHaveClass('u-input', 'u-input--mono')
    })

    it('renders a textarea when asked, with the textarea class', () => {
      renderComponent(<Input id="notes" label="Notes" textarea />)

      const field = screen.getByLabelText('Notes')
      expect(field.tagName).toBe('TEXTAREA')
      expect(field).toHaveClass('u-input', 'u-input--textarea')
    })

    it('wraps the control when a lead or a trail adornment is supplied', () => {
      const { container } = renderComponent(
        <Input id="q" label="Query" lead={<span>L</span>} trail={<span>T</span>} />,
      )

      const wrap = container.querySelector('.u-inputwrap')
      expect(wrap).toHaveClass('u-inputwrap--lead', 'u-inputwrap--trail')
      expect(container.querySelector('.u-inputwrap__lead')).toHaveAttribute('aria-hidden', 'true')
      expect(container.querySelector('.u-inputwrap__trail')).toHaveAttribute('aria-hidden', 'true')
    })

    it('renders the hint under the field and binds it', () => {
      renderComponent(
        <Input
          id="base_url"
          label="Base URL"
          hint="Same-origin only — the console never asks for a server URL for itself."
        />,
      )

      const field = screen.getByLabelText('Base URL')
      const [hint] = describedBy(field)
      expect(hint).toHaveClass('u-field__hint')
      expect(hint).toHaveTextContent('Same-origin only — the console never asks for a server URL for itself.')
    })

    it('carries required as aria-required, leaving native validation off', () => {
      // patterns.md §3: the field-scale error is the server's `validation_failed` message,
      // printed verbatim. Native constraint bubbles would pre-empt it with copy nobody wrote.
      renderComponent(<Input id="username" label="Username" required />)

      const field = screen.getByLabelText('Username')
      expect(field).toHaveAttribute('aria-required', 'true')
      expect(field).not.toHaveAttribute('required')
    })

    it('spreads the rest of the native props onto the control', () => {
      renderComponent(
        <Input id="name" label="Name" data-testid="name-field" name="name" placeholder="Living Room" />,
      )

      const field = screen.getByTestId('name-field')
      expect(field).toHaveAttribute('name', 'name')
      expect(field).toHaveAttribute('placeholder', 'Living Room')
    })

    it('does not branch on density', () => {
      const { container } = renderComponent(<Input id="a" label="A" mono />, { density: 'compact' })

      expect(container.querySelector('.u-input')).toHaveClass('u-input', 'u-input--mono')
    })
  })

  describe('validation_failed', () => {
    // The 422 problem document as the API sends it. `loc` picks the field, `msg` is the copy.
    const problem = {
      code: 'validation_failed',
      status: 422,
      title: 'That request did not validate.',
      detail: 'The body failed validation against the source schema.',
      errors: [
        { loc: ['body', 'base_url'], msg: 'URL scheme must be http or https.' },
        { loc: ['body', 'username'], msg: 'Field required.' },
      ],
    }
    function msgFor(field: string): string {
      const found = problem.errors.find((e) => e.loc.at(-1) === field)
      if (!found) throw new Error(`no errors[] entry whose loc ends in "${field}"`)
      return found.msg
    }

    it('prints errors[].msg verbatim against the field named by errors[].loc', () => {
      renderComponent(<Input id="username" label="Username" error={msgFor('username')} />)

      const field = screen.getByLabelText('Username')
      const [error] = describedBy(field)
      expect(error).toHaveTextContent('Field required.')
      expect(error).toHaveClass('u-field__error')
      // `role="status"`, not `role="alert"`: alert's implicit live-region
      // politeness is *assertive*, and patterns.md §12 is unambiguous that
      // nothing in this product is assertive.
      expect(error).toHaveAttribute('role', 'status')
      expect(error).not.toHaveAttribute('aria-live', 'assertive')
    })

    it('sets aria-invalid and binds the message with aria-describedby', () => {
      renderComponent(<Input id="base_url" label="Base URL" error={msgFor('base_url')} />)

      const field = screen.getByLabelText('Base URL')
      expect(field).toHaveAttribute('aria-invalid', 'true')
      expect(describedBy(field)).toHaveLength(1)
      expect(describedBy(field)[0]).toHaveTextContent('URL scheme must be http or https.')
    })

    it('carries hue, icon and word — never colour alone', () => {
      // patterns.md §12: the bad tone's glyph is fixed at x-circle everywhere in the product.
      const { container } = renderComponent(<Input id="username" label="Username" error="Field required." />)

      expect(container.querySelector('.u-field__error [data-icon="x-circle"]')).not.toBeNull()
      expect(screen.getByRole('status')).toHaveTextContent('Field required.')
    })

    it('replaces the hint with the error and leaves no dangling description', () => {
      renderComponent(
        <Input id="username" label="Username" hint="Usher's own account." error="Field required." />,
      )

      expect(screen.queryByText("Usher's own account.")).toBeNull()
      const described = describedBy(screen.getByLabelText('Username'))
      expect(described).toHaveLength(1)
      expect(described[0]).toHaveTextContent('Field required.')
    })

    it('keeps a consumer-supplied aria-describedby rather than clobbering it', () => {
      renderComponent(
        <>
          <span id="outside">Reachable from the Usher container.</span>
          <Input id="base_url" label="Base URL" aria-describedby="outside" error="Field required." />
        </>,
      )

      const texts = describedBy(screen.getByLabelText('Base URL')).map((n) => n.textContent)
      expect(texts).toEqual(['Reachable from the Usher container.', 'Field required.'])
    })
  })

  describe('behaviour', () => {
    it('types into the field and reports every keystroke', async () => {
      const onChange = vi.fn<() => void>()
      const { user } = renderComponent(<Input id="q" label="Query" onChange={onChange} />)

      await user.type(screen.getByLabelText('Query'), 'sol')

      expect(screen.getByLabelText<HTMLInputElement>('Query').value).toBe('sol')
      expect(onChange).toHaveBeenCalledTimes(3)
    })

    it('types into the textarea variant too', async () => {
      const { user } = renderComponent(<Input id="notes" label="Notes" textarea />)

      await user.type(screen.getByLabelText('Notes'), 'two lines')

      expect(screen.getByLabelText<HTMLTextAreaElement>('Notes').value).toBe('two lines')
    })
  })

  describe('credentials are write-only (§13)', () => {
    const HINT = 'Sent once, stored encrypted on the server, and never returned by the API.'

    it('states where the credential goes, bound to the field', () => {
      renderComponent(<Input id="password" label="Password" type="password" hint={HINT} />)

      const field = screen.getByLabelText('Password')
      expect(field).toHaveAttribute('type', 'password')
      expect(describedBy(field)[0]).toHaveTextContent(HINT)
    })

    it('never renders the value back — not as text, not in an attribute', async () => {
      const secret = 'correct-horse-battery'
      const { container, user } = renderComponent(
        <Input id="password" label="Password" type="password" hint={HINT} />,
      )

      const field = screen.getByLabelText<HTMLInputElement>('Password')
      await user.type(field, secret)

      expect(field.value).toBe(secret)
      expect(field).toHaveAttribute('type', 'password')
      expectValueNeverEchoed(container, field, secret)
    })

    it('never leaks the value into the error treatment either', async () => {
      const secret = 'correct-horse-battery'
      const { container, user } = renderComponent(
        <Input id="password" label="Password" type="password" error="Field required." />,
      )

      const field = screen.getByLabelText<HTMLInputElement>('Password')
      await user.type(field, secret)

      expect(screen.getByRole('status')).toHaveTextContent('Field required.')
      expectValueNeverEchoed(container, field, secret)
    })
  })

  describe('accessibility', () => {
    it('has no violations with a hint', async () => {
      const { container } = renderComponent(
        <Input id="base_url" label="Base URL" mono hint="Reachable from the Usher container." />,
      )

      await expectNoViolations(container)
    })

    it('has no violations in the validation_failed state', async () => {
      const { container } = renderComponent(
        <Input id="username" label="Username" required error="Field required." />,
      )

      await expectNoViolations(container)
    })

    it('has no violations as a password field or as a textarea', async () => {
      const { container } = renderComponent(
        <>
          <Input
            id="password"
            label="Password"
            type="password"
            hint="Stored encrypted on the server, never returned by the API."
          />
          <Input id="notes" label="Notes" textarea />
        </>,
      )

      await expectNoViolations(container)
    })
  })
})
