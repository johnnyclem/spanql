import { retryWithBackoff } from "./retry";

export class Server {
  start(port: number) {
    return listen(port);
  }
}

export function handleCharge(req: Request) {
  return retryWithBackoff(() => charge(req));
}

export const handleRefund = async (req: Request) => {
  return retryWithBackoff(() => refund(req));
};
