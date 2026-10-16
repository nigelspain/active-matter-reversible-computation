#!/usr/bin/env python3
"""
16-Bit Processor Golden Model & RTL Lockstep Retirement Comparator

Architecture Summary:
  - Registers: R0-R7 (16-bit general-purpose).
  - Status Flags: Z (Zero), N (Negative), C (Carry), V (Signed Overflow).
  - Memory: 64 KWords (16-bit word-addressed).
  - Trace Contract:
      CYC=<cycle> PC=<hex> OP=<hex> WR=<0/1> RD=<reg> DATA=<hex> FLAGS=<ZNCV>

Exit Codes:
  0: Success / PASS (all retired instructions matched golden state).
  1: Architectural mismatch detected (fail-fast on first divergence).
  2: Truncation error (stream closed before core reached HALT).
  3: Configuration / file error.
"""

import sys
import re
import argparse
from dataclasses import dataclass
from typing import Optional, Iterable, List, Dict, Tuple


# ==============================================================================
# 1. RETIREMENT RECORD SPECIFICATION
# ==============================================================================

@dataclass
class RetirementRecord:
    cycle: int
    pc: int
    opcode: int
    reg_write: bool
    rd_addr: int
    rd_data: int
    flags: str  # 4-character bitstring: "ZNCV", e.g. "1000"

    def __str__(self) -> str:
        wr_str = f"R{self.rd_addr} <= 0x{self.rd_data:04X}" if self.reg_write else "NO_WRITE    "
        return f"[Cyc {self.cycle:6d}] PC: 0x{self.pc:04X} | Op: 0x{self.opcode:04X} | {wr_str} | Flags: {self.flags}"


# ==============================================================================
# 2. 16-BIT GOLDEN MODEL
# ==============================================================================

class Toy16GoldenModel:
    def __init__(self):
        self.regs = [0] * 8
        self.pc = 0
        self.flag_z = False
        self.flag_n = False
        self.flag_c = False
        self.flag_v = False
        self.memory: Dict[int, int] = {}
        self.halted = False

    def load_program(self, binary: List[int], start_pc: int = 0):
        for idx, word in enumerate(binary):
            self.memory[(start_pc + idx) & 0xFFFF] = word & 0xFFFF
        self.pc = start_pc & 0xFFFF

    def load_memory_dict(self, mem_dict: Dict[int, int], start_pc: int = 0):
        self.memory.update(mem_dict)
        self.pc = start_pc & 0xFFFF

    def get_flag_string(self) -> str:
        return f"{int(self.flag_z)}{int(self.flag_n)}{int(self.flag_c)}{int(self.flag_v)}"

    def _eval_add_flags(self, a: int, b: int, res_raw: int) -> Tuple[bool, bool, bool, bool]:
        res16 = res_raw & 0xFFFF
        z = (res16 == 0)
        n = bool(res16 & 0x8000)
        c = (res_raw > 0xFFFF)
        v = bool((~(a ^ b) & (a ^ res16)) & 0x8000)
        return z, n, c, v

    def _eval_sub_flags(self, a: int, b: int, res_raw: int) -> Tuple[bool, bool, bool, bool]:
        res16 = res_raw & 0xFFFF
        z = (res16 == 0)
        n = bool(res16 & 0x8000)
        c = (a >= b)
        v = bool(((a ^ b) & (a ^ res16)) & 0x8000)
        return z, n, c, v

    def _eval_logic_flags(self, res16: int) -> Tuple[bool, bool, bool, bool]:
        z = (res16 == 0)
        n = bool(res16 & 0x8000)
        return z, n, False, False

    @staticmethod
    def _sign_extend_6(val6: int) -> int:
        val6 = val6 & 0x3F
        return (val6 - 0x40) if (val6 & 0x20) else val6

    def step(self, cycle: int = 0) -> Optional[RetirementRecord]:
        if self.halted:
            return None

        current_pc = self.pc
        instr = self.memory.get(current_pc, 0x0000)
        opcode = (instr >> 12) & 0xF

        rd_r = (instr >> 9) & 0x7
        rs1_r = (instr >> 6) & 0x7
        rs2_r = (instr >> 3) & 0x7

        rs2_s = (instr >> 9) & 0x7
        rs1_s = (instr >> 6) & 0x7

        rs1_b = (instr >> 9) & 0x7
        rs2_b = (instr >> 6) & 0x7

        payload6 = instr & 0x3F
        signed_payload6 = self._sign_extend_6(payload6)

        reg_write = False
        rd_addr = 0
        rd_data = 0
        next_pc = (current_pc + 1) & 0xFFFF

        if opcode == 0x0:  # ADD rd, rs1, rs2
            raw = self.regs[rs1_r] + self.regs[rs2_r]
            rd_data = raw & 0xFFFF
            self.flag_z, self.flag_n, self.flag_c, self.flag_v = self._eval_add_flags(
                self.regs[rs1_r], self.regs[rs2_r], raw
            )
            reg_write, rd_addr = True, rd_r

        elif opcode == 0x1:  # SUB rd, rs1, rs2
            raw = self.regs[rs1_r] - self.regs[rs2_r]
            rd_data = raw & 0xFFFF
            self.flag_z, self.flag_n, self.flag_c, self.flag_v = self._eval_sub_flags(
                self.regs[rs1_r], self.regs[rs2_r], raw
            )
            reg_write, rd_addr = True, rd_r

        elif opcode == 0x2:  # AND rd, rs1, rs2
            rd_data = (self.regs[rs1_r] & self.regs[rs2_r]) & 0xFFFF
            self.flag_z, self.flag_n, self.flag_c, self.flag_v = self._eval_logic_flags(rd_data)
            reg_write, rd_addr = True, rd_r

        elif opcode == 0x3:  # OR rd, rs1, rs2
            rd_data = (self.regs[rs1_r] | self.regs[rs2_r]) & 0xFFFF
            self.flag_z, self.flag_n, self.flag_c, self.flag_v = self._eval_logic_flags(rd_data)
            reg_write, rd_addr = True, rd_r

        elif opcode == 0x4:  # XOR rd, rs1, rs2
            rd_data = (self.regs[rs1_r] ^ self.regs[rs2_r]) & 0xFFFF
            self.flag_z, self.flag_n, self.flag_c, self.flag_v = self._eval_logic_flags(rd_data)
            reg_write, rd_addr = True, rd_r

        elif opcode == 0x5:  # ADDI rd, rs1, imm6
            imm16 = signed_payload6 & 0xFFFF
            raw = self.regs[rs1_r] + imm16
            rd_data = raw & 0xFFFF
            self.flag_z, self.flag_n, self.flag_c, self.flag_v = self._eval_add_flags(
                self.regs[rs1_r], imm16, raw
            )
            reg_write, rd_addr = True, rd_r

        elif opcode == 0x6:  # LOAD rd, offset6(rs1)
            addr = (self.regs[rs1_r] + signed_payload6) & 0xFFFF
            rd_data = self.memory.get(addr, 0x0000)
            reg_write, rd_addr = True, rd_r

        elif opcode == 0x7:  # STORE rs2, offset6(rs1)
            addr = (self.regs[rs1_s] + signed_payload6) & 0xFFFF
            self.memory[addr] = self.regs[rs2_s]
            reg_write = False

        elif opcode == 0x8:  # BEQ rs1, rs2, disp6
            if self.regs[rs1_b] == self.regs[rs2_b]:
                next_pc = (current_pc + 1 + signed_payload6) & 0xFFFF
            reg_write = False

        elif opcode == 0x9:  # BNE rs1, rs2, disp6
            if self.regs[rs1_b] != self.regs[rs2_b]:
                next_pc = (current_pc + 1 + signed_payload6) & 0xFFFF
            reg_write = False

        elif opcode == 0xF:  # HALT
            self.halted = True
            reg_write = False

        else:
            raise ValueError(f"Undefined opcode 0x{opcode:X} at PC 0x{current_pc:04X}")

        if reg_write:
            self.regs[rd_addr] = rd_data

        record = RetirementRecord(
            cycle=cycle,
            pc=current_pc,
            opcode=instr,
            reg_write=reg_write,
            rd_addr=rd_addr if reg_write else 0,
            rd_data=rd_data if reg_write else 0,
            flags=self.get_flag_string()
        )

        self.pc = next_pc
        return record


# ==============================================================================
# 3. TRACE PARSER & COMPARATOR ENGINE
# ==============================================================================

class TraceParser:
    LINE_REGEX = re.compile(
        r"CYC=\s*(?P<cyc>\d+)\s+"
        r"PC=0x(?P<pc>[0-9A-Fa-f]+)\s+"
        r"OP=0x(?P<op>[0-9A-Fa-f]+)\s+"
        r"WR=(?P<wr>[01])\s+"
        r"RD=(?P<rd>\d+)\s+"
        r"DATA=0x(?P<data>[0-9A-Fa-f]+)\s+"
        r"FLAGS=(?P<flags>[01]{4})"
    )

    @classmethod
    def parse_line(cls, line: str) -> Optional[RetirementRecord]:
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            return None
        match = cls.LINE_REGEX.search(line)
        if not match:
            return None
        return RetirementRecord(
            cycle=int(match.group("cyc")),
            pc=int(match.group("pc"), 16),
            opcode=int(match.group("op"), 16),
            reg_write=bool(int(match.group("wr"))),
            rd_addr=int(match.group("rd")),
            rd_data=int(match.group("data"), 16),
            flags=match.group("flags")
        )


class LockstepComparator:
    def __init__(self, golden_model: Toy16GoldenModel):
        self.golden = golden_model
        self.retired_count = 0

    def verify_stream(self, trace_stream: Iterable[str], require_halt: bool = True) -> int:
        """
        Processes streaming trace lines in real time.
        Returns:
          0 on success
          1 on architectural mismatch (fail-fast)
          2 on truncation / missing halt
        """
        print("=" * 88, flush=True)
        print(" STARTING REAL-TIME CO-SIMULATION LOCKSTEP RETIREMENT VERIFICATION", flush=True)
        print("=" * 88, flush=True)

        for line_idx, line in enumerate(trace_stream, start=1):
            rtl_event = TraceParser.parse_line(line)
            if rtl_event is None:
                continue

            expected = self.golden.step(cycle=rtl_event.cycle)
            if expected is None:
                self._report_failure(
                    "RTL strobed retirement but Golden Model has already HALTED.",
                    line_idx, rtl_event, None
                )
                return 1

            self.retired_count += 1

            # Validate architectural state commit
            mismatch_reasons = []
            if rtl_event.pc != expected.pc:
                mismatch_reasons.append(f"PC mismatch (Exp: 0x{expected.pc:04X}, Got: 0x{rtl_event.pc:04X})")
            if rtl_event.opcode != expected.opcode:
                mismatch_reasons.append(f"Opcode mismatch (Exp: 0x{expected.opcode:04X}, Got: 0x{rtl_event.opcode:04X})")
            if rtl_event.reg_write != expected.reg_write:
                mismatch_reasons.append(f"RegWrite mismatch (Exp: {expected.reg_write}, Got: {rtl_event.reg_write})")
            if expected.reg_write:
                if rtl_event.rd_addr != expected.rd_addr:
                    mismatch_reasons.append(f"Rd Addr mismatch (Exp: R{expected.rd_addr}, Got: R{rtl_event.rd_addr})")
                if rtl_event.rd_data != expected.rd_data:
                    mismatch_reasons.append(f"Rd Data mismatch (Exp: 0x{expected.rd_data:04X}, Got: 0x{rtl_event.rd_data:04X})")
            if rtl_event.flags != expected.flags:
                mismatch_reasons.append(f"Flags [ZNCV] mismatch (Exp: {expected.flags}, Got: {rtl_event.flags})")

            if mismatch_reasons:
                self._report_failure("; ".join(mismatch_reasons), line_idx, rtl_event, expected)
                return 1

        if require_halt and not self.golden.halted:
            print("\n" + "!" * 88, flush=True)
            print(" [INCOMPLETE TRACE ERROR] Stream closed but processor did NOT reach HALT state.", flush=True)
            print(f" Retired: {self.retired_count} instructions. Current PC: 0x{self.golden.pc:04X}", flush=True)
            print("!" * 88 + "\n", flush=True)
            return 2

        print("-" * 88, flush=True)
        print(f" [PASS] All {self.retired_count} retired instructions matched golden architectural state.", flush=True)
        print(f" Final Architectural State: Core HALTED at PC: 0x{self.golden.pc:04X}", flush=True)
        print("=" * 88, flush=True)
        return 0

    def _report_failure(
        self,
        reason: str,
        line_num: int,
        rtl: RetirementRecord,
        expected: Optional[RetirementRecord]
    ):
        print("\n" + "!" * 88, flush=True)
        print(f" LOCKSTEP VERIFICATION MISMATCH DETECTED AT TRACE LINE {line_num}", flush=True)
        print("!" * 88, flush=True)
        print(f" Reason: {reason}", flush=True)
        print("-" * 88, flush=True)
        print(f" RTL Retire Event:    {rtl}", flush=True)
        if expected:
            print(f" Golden Expectation:  {expected}", flush=True)
        else:
            print(" Golden Expectation:  None (Core was halted)", flush=True)
        print("-" * 88, flush=True)
        print(" Architectural Register Dump (Golden Model at Failure):", flush=True)
        for i in range(0, 8, 4):
            reg_chunk = "  ".join(f"R{r}: 0x{self.golden.regs[r]:04X}" for r in range(i, i + 4))
            print(f"   {reg_chunk}", flush=True)
        print(f"   Flags: Z={int(self.golden.flag_z)} N={int(self.golden.flag_n)} "
              f"C={int(self.golden.flag_c)} V={int(self.golden.flag_v)}", flush=True)
        print("!" * 88 + "\n", flush=True)


# ==============================================================================
# 4. MEMORY / BINARY LOADERS & ASSEMBLERS
# ==============================================================================

def load_hex_file(filepath: str) -> Dict[int, int]:
    """
    Loads Verilog-style hex memory files (@address supported, comments supported).
    """
    memory = {}
    current_addr = 0
    with open(filepath, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("//") or line.startswith("#"):
                continue
            tokens = line.split()
            for token in tokens:
                if token.startswith("@"):
                    current_addr = int(token[1:], 16)
                else:
                    val = int(token, 16) & 0xFFFF
                    memory[current_addr & 0xFFFF] = val
                    current_addr += 1
    return memory

def asm_r_type(opcode: int, rd: int, rs1: int, rs2: int, subop: int = 0) -> int:
    return ((opcode & 0xF) << 12) | ((rd & 0x7) << 9) | ((rs1 & 0x7) << 6) | ((rs2 & 0x7) << 3) | (subop & 0x7)

def asm_add(rd: int, rs1: int, rs2: int) -> int:
    return asm_r_type(0x0, rd, rs1, rs2)

def asm_addi(rd: int, rs1: int, imm6: int) -> int:
    return (0x5 << 12) | ((rd & 0x7) << 9) | ((rs1 & 0x7) << 6) | (imm6 & 0x3F)

def asm_bne(rs1: int, rs2: int, disp6: int) -> int:
    return (0x9 << 12) | ((rs1 & 0x7) << 9) | ((rs2 & 0x7) << 6) | (disp6 & 0x3F)

def asm_halt() -> int:
    return 0xF000


# ==============================================================================
# 5. SELF-TEST RUNNER
# ==============================================================================

def run_suite() -> int:
    fib_prog = [
        asm_addi(1, 0, 0),     # 0x0000: R1 = 0
        asm_addi(2, 0, 1),     # 0x0001: R2 = 1
        asm_addi(3, 0, 2),     # 0x0002: R3 = 2 (Loop 2 iterations)
        # loop:
        asm_add(4, 1, 2),      # 0x0003: R4 = R1 + R2
        asm_add(1, 2, 0),      # 0x0004: R1 = R2
        asm_add(2, 4, 0),      # 0x0005: R2 = R4
        asm_addi(3, 3, -1),    # 0x0006: R3 = R3 - 1
        asm_bne(3, 0, -5),     # 0x0007: BNE R3, R0, -5
        asm_halt()             # 0x0008: HALT
    ]

    print("\n[TEST 1] Running Standalone Golden Model to HALT...")
    model = Toy16GoldenModel()
    model.load_program(fib_prog)
    cyc = 1
    while not model.halted:
        model.step(cycle=cyc)
        cyc += 1
    print(f"   Golden Model Completed in {cyc - 1} steps. F(2) Result in R2: {model.regs[2]}")
    assert model.regs[2] == 2, "Fibonacci calculation error in Golden Model."

    print("\n[TEST 2] Verifying Synthetic RTL Pipeline Trace...")
    model_lockstep = Toy16GoldenModel()
    model_lockstep.load_program(fib_prog)

    valid_trace = [
        f"CYC=5   PC=0x0000 OP=0x{fib_prog[0]:04X} WR=1 RD=1 DATA=0x0000 FLAGS=1000",
        f"CYC=6   PC=0x0001 OP=0x{fib_prog[1]:04X} WR=1 RD=2 DATA=0x0001 FLAGS=0000",
        f"CYC=7   PC=0x0002 OP=0x{fib_prog[2]:04X} WR=1 RD=3 DATA=0x0002 FLAGS=0000",
        f"CYC=8   PC=0x0003 OP=0x{fib_prog[3]:04X} WR=1 RD=4 DATA=0x0001 FLAGS=0000",
        f"CYC=9   PC=0x0004 OP=0x{fib_prog[4]:04X} WR=1 RD=1 DATA=0x0001 FLAGS=0000",
        f"CYC=10  PC=0x0005 OP=0x{fib_prog[5]:04X} WR=1 RD=2 DATA=0x0001 FLAGS=0000",
        f"CYC=11  PC=0x0006 OP=0x{fib_prog[6]:04X} WR=1 RD=3 DATA=0x0001 FLAGS=0010",
        f"CYC=13  PC=0x0007 OP=0x{fib_prog[7]:04X} WR=0 RD=0 DATA=0x0000 FLAGS=0010",
        f"CYC=14  PC=0x0003 OP=0x{fib_prog[3]:04X} WR=1 RD=4 DATA=0x0002 FLAGS=0000",
        f"CYC=15  PC=0x0004 OP=0x{fib_prog[4]:04X} WR=1 RD=1 DATA=0x0001 FLAGS=0000",
        f"CYC=16  PC=0x0005 OP=0x{fib_prog[5]:04X} WR=1 RD=2 DATA=0x0002 FLAGS=0000",
        f"CYC=17  PC=0x0006 OP=0x{fib_prog[6]:04X} WR=1 RD=3 DATA=0x0000 FLAGS=1010",
        f"CYC=18  PC=0x0007 OP=0x{fib_prog[7]:04X} WR=0 RD=0 DATA=0x0000 FLAGS=1010",
        f"CYC=19  PC=0x0008 OP=0x{fib_prog[8]:04X} WR=0 RD=0 DATA=0x0000 FLAGS=1010",
    ]

    comp = LockstepComparator(model_lockstep)
    rc = comp.verify_stream(valid_trace, require_halt=True)
    assert rc == 0, "Lockstep verification failed on valid trace."

    print("\n[TEST 3] Verifying Trace Truncation Guard...")
    model_trunc = Toy16GoldenModel()
    model_trunc.load_program(fib_prog)
    comp_trunc = LockstepComparator(model_trunc)
    rc_trunc = comp_trunc.verify_stream(valid_trace[:5], require_halt=True)
    assert rc_trunc == 2, "Truncation guard failed to return code 2."

    print("\n[TEST 4] Verifying Error Detection on Injected Flag Fault...")
    model_fault = Toy16GoldenModel()
    model_fault.load_program(fib_prog)
    corrupted_trace = list(valid_trace)
    corrupted_trace[6] = f"CYC=11  PC=0x0006 OP=0x{fib_prog[6]:04X} WR=1 RD=3 DATA=0x0001 FLAGS=0000"
    comp_fault = LockstepComparator(model_fault)
    rc_fault = comp_fault.verify_stream(corrupted_trace, require_halt=True)
    assert rc_fault == 1, "Comparator failed to return code 1 on mismatch."

    print("\nAll internal test suites passed cleanly.", flush=True)
    return 0


# ==============================================================================
# 6. CLI ENTRY POINT
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="16-Bit Processor RTL Lockstep Retirement Comparator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run live piped RTL trace against compiled machine code:
  ./Vcore_top +trace | python lockstep_comparator.py --stdin --bin firmware.hex

  # Run built-in self-tests:
  python lockstep_comparator.py --test
        """
    )
    parser.add_argument("--stdin", action="store_true", help="Stream trace directly from sys.stdin.")
    parser.add_argument("--bin", type=str, help="Path to program memory hex file.")
    parser.add_argument("--start-pc", type=lambda x: int(x, 0), default=0, help="Initial PC (default: 0x0000).")
    parser.add_argument("--no-require-halt", action="store_true", help="Do not fail if stream terminates before HALT.")
    parser.add_argument("--test", action="store_true", help="Execute built-in verification self-tests.")

    args = parser.parse_args()

    if args.test or (len(sys.argv) == 1):
        sys.exit(run_suite())

    if args.stdin:
        model = Toy16GoldenModel()
        if args.bin:
            try:
                mem_image = load_hex_file(args.bin)
                model.load_memory_dict(mem_image, start_pc=args.start_pc)
            except Exception as e:
                print(f"Error loading binary file '{args.bin}': {e}", file=sys.stderr, flush=True)
                sys.exit(3)
        else:
            # If no binary specified, default to built-in demonstration program
            fib_prog = [
                asm_addi(1, 0, 0), asm_addi(2, 0, 1), asm_addi(3, 0, 2),
                asm_add(4, 1, 2),  asm_add(1, 2, 0),  asm_add(2, 4, 0),
                asm_addi(3, 3, -1),asm_bne(3, 0, -5), asm_halt()
            ]
            model.load_program(fib_prog, start_pc=args.start_pc)

        comparator = LockstepComparator(model)
        exit_code = comparator.verify_stream(sys.stdin, require_halt=not args.no_require_halt)
        sys.exit(exit_code)

    parser.print_help()
    sys.exit(3)


if __name__ == "__main__":
    main()
