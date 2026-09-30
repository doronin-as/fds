"""Compile the production DUMP_TIMERS routine and test repeated MPI dumps."""

import argparse
from pathlib import Path
import re
import subprocess
import tempfile


def driver(source):
    match = re.search(
        r"^SUBROUTINE DUMP_TIMERS\s*$.*?^END SUBROUTINE DUMP_TIMERS\s*$",
        source, re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise ValueError("DUMP_TIMERS not found")
    return r'''
program test_dump_timers
use mpi
implicit none
integer, parameter :: EB = kind(1.d0), N_TIMERS = 16, LU_CPU = 20
integer :: MY_RANK, N_MPI_PROCESSES, IERR, I, N, STATUS(MPI_STATUS_SIZE)
integer :: dump_number, row_rank, row_number
real(EB) :: T_USED(N_TIMERS), before(N_TIMERS), now, values(N_TIMERS+1)
real(EB) :: expected_comm, expected_pack, expected_main, expected_total
character(256) :: CHID = 'timer_regression', FN_CPU
character(1024) :: text_line

call MPI_INIT(IERR)
call MPI_COMM_RANK(MPI_COMM_WORLD, MY_RANK, IERR)
call MPI_COMM_SIZE(MPI_COMM_WORLD, N_MPI_PROCESSES, IERR)
T_USED = 2._EB
T_USED(1) = 100._EB
T_USED(11) = 20._EB
T_USED(12) = 5._EB
now = 200._EB
do dump_number = 1,3
   if (dump_number == 3) then
      T_USED(11) = T_USED(11) + 7._EB
      T_USED(12) = T_USED(12) + 2._EB
      now = 210._EB
   endif
   before = T_USED
   call DUMP_TIMERS
   if (any(T_USED /= before)) call fail('Accumulated timers changed during output')
   if (dump_number < 3) then
      expected_comm = 15._EB
      expected_pack = 5._EB
      expected_main = 54._EB
      expected_total = 100._EB
   else
      expected_comm = 20._EB
      expected_pack = 7._EB
      expected_main = 57._EB
      expected_total = 110._EB
   endif
   if (MY_RANK == 0) then
      open(LU_CPU, file=FN_CPU, status='old', action='read')
      read(LU_CPU,'(A)') text_line
      do row_number = 0,N_MPI_PROCESSES-1
         read(LU_CPU,'(A)') text_line
         read(text_line,*) row_rank, values
         if (row_rank /= row_number) call fail('Incorrect rank in CSV output')
         if (abs(values(1)-expected_main) > 1.e-9_EB) call fail('Incorrect MAIN time')
         if (abs(values(11)-expected_comm) > 1.e-9_EB) call fail('Incorrect COMM time')
         if (abs(values(12)-expected_pack) > 1.e-9_EB) call fail('Incorrect PACK time')
         if (abs(values(17)-expected_total) > 1.e-9_EB) call fail('Incorrect total time')
         do I=2,N_TIMERS
            if (I==11 .or. I==12) cycle
            if (abs(values(I)-2._EB) > 1.e-9_EB) call fail('Other timer changed')
         enddo
      enddo
      close(LU_CPU)
   endif
   call MPI_BARRIER(MPI_COMM_WORLD,IERR)
enddo
if (MY_RANK == 0) print *, 'PASS: repeated timer dumps preserve accumulators and CSV totals'
call MPI_FINALIZE(IERR)
contains
function CURRENT_TIME() result(t)
real(EB) :: t
t = now
end function CURRENT_TIME
subroutine fail(message)
character(*), intent(in) :: message
print *, message
call MPI_ABORT(MPI_COMM_WORLD,1,IERR)
end subroutine fail
''' + match.group(0) + '\nend program test_dump_timers\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path,
                        default=Path(__file__).resolve().parents[2] / 'Source/main.f90')
    parser.add_argument('--emit-source', type=Path,
                        help='Write the standalone Fortran driver without executing it')
    args = parser.parse_args()
    generated = driver(args.source.read_text(encoding='utf-8'))
    if args.emit_source:
        args.emit_source.write_text(generated, encoding='utf-8')
        return
    with tempfile.TemporaryDirectory(prefix='fds-timers-') as temp:
        directory = Path(temp)
        source_path = directory / 'test.f90'
        source_path.write_text(generated, encoding='utf-8')
        executable = directory / 'test_dump_timers'
        subprocess.run(['mpifort', '-O0', '-g', '-fcheck=all', '-Wall',
                        str(source_path), '-o', str(executable)], check=True)
        for ranks in (1, 2):
            subprocess.run(['mpirun', '--oversubscribe', '-np', str(ranks),
                            str(executable)], cwd=directory, check=True, timeout=60)


if __name__ == '__main__':
    main()
