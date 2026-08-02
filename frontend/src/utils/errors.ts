import axios from "axios";

/**
 * Shape of a FastAPI error body. `detail` is a plain string for
 * `HTTPException(detail=...)` and an array of pydantic validation errors
 * for 422 responses. The exact contract wasn't specified by the auth PRP,
 * so this assumes FastAPI's default error envelope.
 */
interface FastAPIValidationError {
  loc: Array<string | number>;
  msg: string;
  type: string;
}

interface FastAPIErrorBody {
  detail?: string | FastAPIValidationError[];
}

export function extractErrorMessage(
  error: unknown,
  fallback = "Something went wrong. Please try again.",
): string {
  if (axios.isAxiosError<FastAPIErrorBody>(error)) {
    const detail = error.response?.data?.detail;
    if (typeof detail === "string" && detail.length > 0) {
      return detail;
    }
    if (Array.isArray(detail) && detail.length > 0) {
      return detail.map((item) => item.msg).join(" ");
    }
    if (error.message) {
      return error.message;
    }
  }
  return fallback;
}
