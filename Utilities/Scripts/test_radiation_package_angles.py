"""Check the production angle-count helper against the original package sizing."""
import argparse
from pathlib import Path
import re
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--emit-source', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    source = (root / 'Source/main.f90').read_text(encoding='utf-8')
    routine = re.search(
        r'^SUBROUTINE RADIATION_PACKAGE_ANGLE_COUNTS\(NRA_BY_IOR\).*?'
        r'^END SUBROUTINE RADIATION_PACKAGE_ANGLE_COUNTS',
        source, re.MULTILINE | re.DOTALL,
    ).group(0)
    program = '''program test_angles
implicit none
integer, parameter :: EB=kind(1.d0)
integer :: NUMBER_RADIATION_ANGLES,ANGLE_INCREMENT,counts(-3:3)
integer :: i,j,n,ior,pattern
integer, parameter :: sizes(6)=[1,5,7,16,100,325],increments(6)=[1,2,3,7,16,400]
real(EB), allocatable :: DLN(:,:)
do i=1,size(sizes)
 NUMBER_RADIATION_ANGLES=sizes(i)
 allocate(DLN(-3:3,NUMBER_RADIATION_ANGLES))
 do j=1,size(increments)
  ANGLE_INCREMENT=increments(j)
  do pattern=1,4
   do ior=-3,3
    do n=1,NUMBER_RADIATION_ANGLES
     select case(pattern)
     case(1)
      DLN(ior,n)=1._EB
     case(2)
      DLN(ior,n)=-1._EB
     case(3)
      DLN(ior,n)=0._EB
     case(4)
      DLN(ior,n)=real(modulo(n*ior,5)-2,EB)
     end select
     if (ior==0) DLN(ior,n)=0._EB
    enddo
   enddo
   call RADIATION_PACKAGE_ANGLE_COUNTS(counts)
   write(*,*) NUMBER_RADIATION_ANGLES,ANGLE_INCREMENT,pattern,counts
  enddo
 enddo
 deallocate(DLN)
enddo
contains
''' + routine + '\nend program test_angles\n'
    if args.emit_source:
        args.emit_source.write_text(program, encoding='utf-8')
        return
    with tempfile.TemporaryDirectory(prefix='fds-angles-') as temp:
        directory = Path(temp)
        driver = directory / 'driver.f90'
        driver.write_text(program, encoding='utf-8')
        executable = directory / 'test_angles'
        subprocess.run(['gfortran', '-O0', '-fcheck=all', '-Wall',
                        str(driver), '-o', str(executable)], check=True)
        output = subprocess.check_output([str(executable)], text=True)
    rows = output.strip().splitlines()
    assert len(rows) == 144, 'Missing angle test cases'
    for row in rows:
        n_angles, increment, pattern, *counts = map(int, row.split())
        for ior, actual in zip(range(-3, 4), counts):
            signs = [0 if ior == 0 else
                     (1 if pattern == 1 else -1 if pattern == 2 else
                      0 if pattern == 3 else (n * ior) % 5 - 2)
                     for n in range(1, n_angles + 1)]
            expected = max(sum(signs[n-1] > 0 for n in
                               range(n_angles-aic+1, 0, -increment))
                           for aic in range(1, increment + 1))
            assert actual == expected, (n_angles, increment, pattern, ior, actual, expected)
    print('PASS: 1008 orientation counts match original package sizing')


if __name__ == '__main__':
    main()
