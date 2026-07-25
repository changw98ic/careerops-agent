import { vi } from 'vitest'

// Stub ant-design-vue CSS import
vi.mock('ant-design-vue/dist/reset.css', () => ({}))

// Mock window.matchMedia (required by ant-design-vue)
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((query) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })),
})

// Mock ResizeObserver (required by ant-design-vue)
globalThis.ResizeObserver = class ResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

// Mock getComputedStyle for ant-design-vue
const origGetComputedStyle = window.getComputedStyle
window.getComputedStyle = (elt, pseudoElt) => {
  const style = origGetComputedStyle(elt, pseudoElt)
  return style
}
