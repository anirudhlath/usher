import { describe, expect, it } from 'vitest'
import { act, renderComponent, screen } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import {
  Artwork,
  IMAGE_LADDER,
  imageProxySizes,
  imageProxySrcSet,
  imageProxyUrl,
  snapImageWidth,
} from './index'

/** No `!` anywhere in this suite; this is the type-safe way to reach the element under test. */
function imgIn(container: HTMLElement): HTMLImageElement {
  const found = container.querySelector('img')
  if (!(found instanceof HTMLImageElement)) throw new Error('expected an <img> to be rendered')
  return found
}

/** Every integer this component prints must be a rung. `abc` carries no digits of its own. */
function numbersIn(text: string): number[] {
  return [...text.matchAll(/\d+/g)].map((match) => Number(match[0]))
}

describe('the image ladder', () => {
  it('snaps every width up to the next rung and never invents one', () => {
    expect(snapImageWidth(1)).toBe(154)
    expect(snapImageWidth(154)).toBe(154)
    expect(snapImageWidth(155)).toBe(342)
    expect(snapImageWidth(342)).toBe(342)
    expect(snapImageWidth(400)).toBe(780)
    expect(snapImageWidth(780)).toBe(780)
    expect(snapImageWidth(781)).toBe(1280)
    expect(snapImageWidth(1280)).toBe(1280)
    expect(snapImageWidth(4000)).toBe(1280)
    expect(snapImageWidth(0)).toBe(154)
    expect(snapImageWidth(-10)).toBe(154)
    expect(snapImageWidth(Number.NaN)).toBe(342)
  })

  it('has exactly four rungs', () => {
    expect([...IMAGE_LADDER]).toEqual([154, 342, 780, 1280])
  })

  it('builds the URL with `w`, not `width` — FastAPI ignores an undeclared param and serves the 342 default', () => {
    expect(imageProxyUrl('abc', 342)).toBe('/images/abc?w=342')
    expect(imageProxyUrl('abc', 400)).toBe('/images/abc?w=780')
    expect(imageProxyUrl('abc', 342)).not.toMatch(/width=/)
  })

  it('escapes the id rather than pasting it into the path', () => {
    expect(imageProxyUrl('a/b?c', 154)).toBe('/images/a%2Fb%3Fc?w=154')
  })

  it('offers only rungs at or above the requested width', () => {
    expect(imageProxySrcSet('abc', 342)).toBe(
      '/images/abc?w=342 342w, /images/abc?w=780 780w, /images/abc?w=1280 1280w',
    )
    expect(imageProxySrcSet('abc', 1280)).toBe('/images/abc?w=1280 1280w')
    expect(imageProxySrcSet('abc', 100)).toContain('/images/abc?w=154 154w')
  })

  it('states sizes as a rung, so no non-ladder number is ever emitted', () => {
    expect(imageProxySizes(400)).toBe('780px')
    expect(numbersIn(imageProxySizes(400))).toEqual([780])
  })
})

describe('Artwork', () => {
  it('renders the proxied image at the snapped rung with a ladder srcSet and sizes', () => {
    const { container } = renderComponent(<Artwork id="abc" kind="poster" width={342} alt="" />)
    const img = imgIn(container)

    expect(img.getAttribute('src')).toBe('/images/abc?w=342')
    expect(img.getAttribute('srcset')).toBe(
      '/images/abc?w=342 342w, /images/abc?w=780 780w, /images/abc?w=1280 1280w',
    )
    expect(img.getAttribute('sizes')).toBe('342px')
    expect(img).toHaveAttribute('loading', 'lazy')
    expect(img).toHaveAttribute('decoding', 'async')
  })

  it('emits ladder widths and nothing else, for a width that is not itself a rung', () => {
    const { container } = renderComponent(<Artwork id="abc" width={400} />)
    const img = imgIn(container)
    const emitted = `${img.getAttribute('src') ?? ''} ${img.getAttribute('srcset') ?? ''} ${img.getAttribute('sizes') ?? ''}`

    expect(numbersIn(emitted).length).toBeGreaterThan(0)
    for (const value of numbersIn(emitted)) {
      expect(IMAGE_LADDER).toContain(value)
    }
    expect(emitted).not.toMatch(/400/)
    expect(emitted).not.toMatch(/width=/)
  })

  it.each([
    ['poster', 'u-art--poster'],
    ['profile', 'u-art--poster'],
    ['backdrop', 'u-art--backdrop'],
    ['still', 'u-art--backdrop'],
    ['logo', 'u-art--square'],
  ] as const)('takes its aspect ratio from kind=%s — the API carries no dimensions', (kind, expected) => {
    const { container } = renderComponent(<Artwork id="abc" kind={kind} />)
    expect(container.querySelector('.u-art')).toHaveClass(expected)
  })

  it('shimmers while the bytes are in flight and stops once they land', () => {
    const { container } = renderComponent(<Artwork id="abc" />)
    expect(container.querySelector('.u-art')).toHaveClass('u-art--loading')

    // Not a user interaction, so `userEvent` cannot express it: this is the network answering.
    act(() => imgIn(container).dispatchEvent(new Event('load')))

    expect(container.querySelector('.u-art')).not.toHaveClass('u-art--loading')
    expect(imgIn(container)).toHaveStyle({ opacity: '1' })
  })

  it('resets to loading when an SSE patch hands a mounted card its first artwork', () => {
    const { container, rerender } = renderComponent(<Artwork id="abc" />)
    act(() => imgIn(container).dispatchEvent(new Event('load')))
    expect(container.querySelector('.u-art')).not.toHaveClass('u-art--loading')

    rerender(<Artwork id="def" />)

    expect(imgIn(container).getAttribute('src')).toBe('/images/def?w=342')
    expect(container.querySelector('.u-art')).toHaveClass('u-art--loading')
  })

  describe('the three absent and failure states', () => {
    it('says "No artwork on record" with the name initial when there is no id', () => {
      const { container } = renderComponent(<Artwork id={null} name="Solaris" />)

      expect(screen.getByText('No artwork on record')).toBeInTheDocument()
      expect(screen.getByText('S')).toBeInTheDocument()
      expect(container.querySelector('img')).toBeNull()
      expect(container.querySelector('.u-art__fallback')).not.toHaveClass('u-art__fallback--retry')
    })

    it('treats an absent key and a null the same way — both mean no artwork on record', () => {
      renderComponent(<Artwork name="Solaris" />)
      expect(screen.getByText('No artwork on record')).toBeInTheDocument()
    })

    it('falls back to "?" when nothing names the thing', () => {
      renderComponent(<Artwork id={null} />)
      expect(screen.getByText('?')).toBeInTheDocument()
    })

    it('says "Artwork unavailable" when the proxy declines (404)', () => {
      renderComponent(<Artwork id="abc" status="declined" name="Mirror" />)

      expect(screen.getByText('Artwork unavailable')).toBeInTheDocument()
      expect(screen.getByText('M')).toBeInTheDocument()
    })

    it('says the source is down in warn tone, with an icon, for 503 + Retry-After', () => {
      const { container } = renderComponent(<Artwork id="abc" status="retry" name="Mirror" />)

      expect(screen.getByText('Artwork source is down. Retrying in 5 s.')).toBeInTheDocument()
      expect(container.querySelector('.u-art__fallback')).toHaveClass('u-art__fallback--retry')
      // patterns.md §12: the warn hue is never the only carrier — hue + icon + word.
      expect(container.querySelector('[data-icon="alert-triangle"]')).toBeInTheDocument()
    })

    it('does not ask the proxy for bytes it has been told will fail', () => {
      const { container } = renderComponent(<Artwork id="abc" status="declined" />)
      expect(container.querySelector('img')).toBeNull()
    })

    it('degrades to the declined sentence when the request itself errors', () => {
      const { container } = renderComponent(<Artwork id="abc" name="Mirror" />)

      act(() => imgIn(container).dispatchEvent(new Event('error')))

      expect(screen.getByText('Artwork unavailable')).toBeInTheDocument()
    })
  })

  it('bypasses the proxy for fixtures, and a fixture never draws a fallback', () => {
    const { container } = renderComponent(<Artwork id={null} srcOverride="/fixture.png" name="Stalker" />)
    const img = imgIn(container)

    expect(img.getAttribute('src')).toBe('/fixture.png')
    expect(img.getAttribute('srcset')).toBeNull()
    expect(container.querySelector('.u-art__fallback')).toBeNull()
  })

  describe('accessibility', () => {
    it('has no violations with an image', async () => {
      const { container } = renderComponent(<Artwork id="abc" alt="" />)
      await expectNoViolations(container)
    })

    it('has no violations in any failure state', async () => {
      const { container } = renderComponent(
        <>
          <Artwork id={null} name="Solaris" />
          <Artwork id="abc" status="declined" name="Mirror" />
          <Artwork id="abc" status="retry" name="Mirror" />
        </>,
      )
      await expectNoViolations(container)
    })

    it('is decorative when the adjacent title text already names the thing', () => {
      const { container } = renderComponent(<Artwork id="abc" alt="" />)
      expect(imgIn(container)).toHaveAttribute('alt', '')
      expect(screen.queryByRole('img')).toBeNull()
    })

    it('takes a real name when it is the only thing naming the artwork', () => {
      renderComponent(<Artwork id="abc" alt="Poster for Stalker" />)
      expect(screen.getByRole('img', { name: 'Poster for Stalker' })).toBeInTheDocument()
    })
  })

  describe('anti-patterns', () => {
    it('never shows a broken-image glyph — every failure is a sentence', () => {
      for (const props of [
        { id: null },
        { id: 'abc', status: 'declined' as const },
        { id: 'abc', status: 'retry' as const },
      ]) {
        const { container, unmount } = renderComponent(<Artwork {...props} name="Mirror" />)
        const fallback = container.querySelector('.u-art__fallback')

        expect(fallback).not.toBeNull()
        expect(fallback?.textContent ?? '').toMatch(/[a-z]{3,}/)
        unmount()
      }
    })

    it('never takes a URL in `id` — the id is pathname-escaped, so a URL cannot smuggle a host in', () => {
      const { container } = renderComponent(<Artwork id="https://evil.example/x.png" />)
      const src = imgIn(container).getAttribute('src') ?? ''

      expect(src.startsWith('/images/')).toBe(true)
      expect(src).not.toContain('//evil.example')
    })
  })
})
