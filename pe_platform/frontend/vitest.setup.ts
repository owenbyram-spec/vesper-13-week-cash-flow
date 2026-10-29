import "@testing-library/jest-dom/vitest";
import * as matchers from "vitest-axe/matchers";
import { afterEach, expect } from "vitest";
import { cleanup } from "@testing-library/react";

// Test-runner setup (LD-T10). Registers:
//  - @testing-library/jest-dom DOM matchers (toBeInTheDocument, etc.)
//  - vitest-axe a11y matchers (toHaveNoViolations) for the accessibility lane
//  - an after-each unmount so component trees don't leak between tests
expect.extend(matchers);

afterEach(() => {
  cleanup();
});
