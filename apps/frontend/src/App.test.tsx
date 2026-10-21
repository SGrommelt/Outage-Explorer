import { render, screen } from "@testing-library/react";
import App from "./App";

test("renders the starter page", () => {
  render(<App />);
  expect(screen.getByRole("heading", { name: "Outage Explorer", level: 1 })).toBeVisible();
});
