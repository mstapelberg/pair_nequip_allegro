import os
import sys
import tempfile
import subprocess
from pathlib import Path
import numpy as np
import textwrap
from io import StringIO
from collections import Counter

import ase
import ase.units
import ase.build
import ase.io

import torch

from nequip.data import AtomicDataDict, from_ase, compute_neighborlist_
from nequip.nn import with_edge_vectors_

from conftest import _check_and_print, COMPILE_MODES
import pytest


@pytest.mark.parametrize(
    "compile_mode",
    # i.e. torchscript or aotinductor
    list(COMPILE_MODES.keys()),
)
@pytest.mark.parametrize(
    "device", ["cpu"] + (["cuda"] if torch.cuda.is_available() else [])
)
def test_repro(deployed_nequip_model, compile_mode: str, device: str):

    structure: ase.Atoms
    model_tmpdir, calc, structures, config, tol = deployed_nequip_model
    model_file_path = model_tmpdir + f"/{device}_" + COMPILE_MODES[compile_mode]

    num_types = len(config["chemical_symbols"])

    newline = "\n"
    periodic = all(structures[0].pbc)
    PRECISION_CONST: float = 1e6
    lmp_in = textwrap.dedent(
        f"""
        units		metal
        atom_style	atomic
        newton off
        thermo 1

        # get a box defined before pair_coeff
        {'boundary p p p' if periodic else 'boundary s s s'}

        read_data structure.data

        pair_style	nequip
        # note that ASE outputs lammps types in alphabetical order of chemical symbols
        # since we use chem symbols in this test, just put the same
        pair_coeff	* * {model_file_path} {' '.join(sorted(set(config["chemical_symbols"])))}
{newline.join('        mass  %i 1.0' % i for i in range(1, num_types + 1))}

        neighbor	1.0 bin
        neigh_modify    delay 0 every 1 check no

        fix		1 all nve

        timestep	0.001

        compute atomicenergies all pe/atom
        compute totalatomicenergy all reduce sum c_atomicenergies
        compute stress all pressure NULL virial  # NULL means without temperature contribution

        thermo_style custom step time temp pe c_totalatomicenergy etotal press spcpu cpuremain c_stress[*]
        run 0
        print "$({PRECISION_CONST} * c_stress[1]) $({PRECISION_CONST} * c_stress[2]) $({PRECISION_CONST} * c_stress[3]) $({PRECISION_CONST} * c_stress[4]) $({PRECISION_CONST} * c_stress[5]) $({PRECISION_CONST} * c_stress[6])" file stress.dat
        print $({PRECISION_CONST} * pe) file pe.dat
        print $({PRECISION_CONST} * c_totalatomicenergy) file totalatomicenergy.dat
        write_dump all custom output.dump id type x y z fx fy fz c_atomicenergies modify format float %20.15g
        """
    )

    # for each model,structure pair
    # build a LAMMPS input using that structure
    with tempfile.TemporaryDirectory() as tmpdir:
        # save out the LAMMPS input:
        print(f"\n\n\n\nTMPDIR\n{tmpdir}\n\n\n\n\n\n")
        infile_path = tmpdir + "/test_repro.in"
        with open(infile_path, "w") as f:
            f.write(lmp_in)
        # environment variables
        env = dict(os.environ)
        env["_NEQUIP_LOG_LEVEL"] = "DEBUG"
        if device == "cpu":
            env["CUDA_VISIBLE_DEVICES"] = ""
        # save out the structure
        for i, structure in enumerate(structures):
            ase.io.write(
                tmpdir + "/structure.data",
                structure,
                format="lammps-data",
            )

            # run LAMMPS
            retcode = subprocess.run(
                [env.get("LAMMPS", "lmp"), "-in", infile_path],
                cwd=tmpdir,
                env=env,
                stdout=subprocess.PIPE,
                stderr=sys.stderr,
            )
            # uncomment to view LAMMPS output
            print(retcode.stdout.decode("ascii"))
            _check_and_print(retcode)

            # load debug data:
            mi = None
            lammps_stdout = iter(retcode.stdout.decode("utf-8").splitlines())
            line = next(lammps_stdout, None)
            while line is not None:
                if line.startswith("NEQUIP edges: i j xi[:] xj[:] cell_shift[:] rij"):
                    edges = []
                    while not line.startswith("end NEQUIP edges"):
                        line = next(lammps_stdout)
                        edges.append(line)
                    edges = np.loadtxt(StringIO("\n".join(edges[:-1])))
                    mi = edges
                elif line.startswith("cell:"):
                    cell = np.loadtxt([next(lammps_stdout) for _ in range(3)])
                    if structure.cell.orthorhombic and any(structure.pbc):
                        # only check it if ortho, since if not it gets rotated
                        assert np.allclose(cell, structure.cell)
                    break
                line = next(lammps_stdout)
            mi = {
                "i": mi[:, 0:1].astype(int),
                "j": mi[:, 1:2].astype(int),
                "xi": mi[:, 2:5],
                "xj": mi[:, 5:8],
                "cell_shift": mi[:, 8:11].astype(int),
                "rij": mi[:, 11:],
            }

            # first, check the model INPUTS
            structure_data = from_ase(structure)
            structure_data = compute_neighborlist_(
                structure_data, r_max=float(config.cutoff_radius)
            )
            structure_data = with_edge_vectors_(structure_data, with_lengths=True)
            lammps_edge_tuples = [
                tuple(e)
                for e in np.hstack(
                    (
                        mi["i"],
                        mi["j"],
                        mi["cell_shift"],
                    )
                )
            ]
            nq_edge_tuples = [
                tuple(e.tolist())
                for e in torch.hstack(
                    (
                        structure_data[AtomicDataDict.EDGE_INDEX_KEY].t(),
                        structure_data[AtomicDataDict.EDGE_CELL_SHIFT_KEY].to(
                            torch.long
                        ),
                    )
                )
            ]
            # same num edges
            assert len(lammps_edge_tuples) == len(nq_edge_tuples)
            # edge i,j,shift tuples should be unique
            assert len(set(lammps_edge_tuples)) == len(mi["i"])
            assert len(set(nq_edge_tuples)) == len(nq_edge_tuples)
            # check same number of i,j edges across both
            assert Counter(e[:2] for e in lammps_edge_tuples) == Counter(
                e[:2] for e in nq_edge_tuples
            )
            if structure.cell.orthorhombic:
                # triclinic cells get modified for lammps
                # check that positions are the same
                # 1e-4 because that's the precision LAMMPS is printing in
                assert np.allclose(
                    mi["xi"], structure.positions[mi["i"].reshape(-1)], atol=1e-6
                )
                assert np.allclose(
                    mi["xj"], structure.positions[mi["j"].reshape(-1)], atol=1e-6
                )
            # check the ij,shift tuples
            # these are NOT changed by the rigid rotate+shift LAMMPS cell transform
            assert set(lammps_edge_tuples) == set(nq_edge_tuples)
            # finally, check for each ij whether the the "sets" of edge lengths match
            nq_ijr = np.core.records.fromarrays(
                (
                    structure_data[AtomicDataDict.EDGE_INDEX_KEY][0],
                    structure_data[AtomicDataDict.EDGE_INDEX_KEY][1],
                    structure_data[AtomicDataDict.EDGE_LENGTH_KEY],
                ),
                names="i,j,rij",
            )
            # we can do "set" comparisons by sorting into groups by ij,
            # and then sorting the rij _within_ each ij pair---
            # this is what `order` does for us with the record array
            nq_ijr.sort(order=["i", "j", "rij"])
            lammps_ijr = np.core.records.fromarrays(
                (
                    mi["i"].reshape(-1),
                    mi["j"].reshape(-1),
                    mi["rij"].reshape(-1),
                ),
                names="i,j,rij",
            )
            lammps_ijr.sort(order=["i", "j", "rij"])
            assert np.allclose(nq_ijr["rij"], lammps_ijr["rij"])

            # --- now check the OUTPUTS ---
            # load dumped data
            lammps_result = ase.io.read(
                tmpdir + "/output.dump", format="lammps-dump-text"
            )
            structure.calc = calc

            # check output atomic quantities
            max_force_err = np.max(
                np.abs(structure.get_forces() - lammps_result.get_forces())
            )
            max_force_comp = np.max(np.abs(structure.get_forces()))
            force_rms = np.sqrt(np.mean(np.square(structure.get_forces())))
            assert np.allclose(
                structure.get_forces(),
                lammps_result.get_forces(),
                atol=tol,
                rtol=tol,
            ), f"Max force error: {max_force_err}, Max force component: {max_force_comp}, Force RMS: {force_rms}"
            assert np.allclose(
                structure.get_potential_energies(),
                lammps_result.arrays["c_atomicenergies"].reshape(-1),
                atol=tol,
                rtol=tol,
            )

            # check system quantities
            lammps_pe = float(Path(tmpdir + "/pe.dat").read_text()) / PRECISION_CONST
            lammps_totalatomicenergy = (
                float(Path(tmpdir + "/totalatomicenergy.dat").read_text())
                / PRECISION_CONST
            )
            # in `metal` units, pressure/stress has units bars
            # so need to convert
            lammps_stress = np.fromstring(
                Path(tmpdir + "/stress.dat").read_text(), sep=" ", dtype=np.float64
            ) * (ase.units.bar / PRECISION_CONST)
            # https://docs.lammps.org/compute_pressure.html
            # > The ordering of values in the symmetric pressure tensor is as follows: pxx, pyy, pzz, pxy, pxz, pyz.
            lammps_stress = np.array(
                [
                    [lammps_stress[0], lammps_stress[3], lammps_stress[4]],
                    [lammps_stress[3], lammps_stress[1], lammps_stress[5]],
                    [lammps_stress[4], lammps_stress[5], lammps_stress[2]],
                ]
            )
            assert np.allclose(lammps_pe, lammps_totalatomicenergy)
            assert np.allclose(
                structure.get_potential_energy(),
                lammps_pe,
                atol=tol,
                rtol=tol,
            )
            if periodic:
                # In LAMMPS, the convention is that the stress tensor, and thus the pressure, is related to the virial
                # WITHOUT a sign change.  In `nequip`, we chose currently to follow the virial = -stress x volume
                # convention => stress = -1/V * virial.  ASE does not change the sign of the virial, so we have
                # to flip the sign from ASE for the comparison.
                ase_stress = -structure.get_stress(voigt=False)
                err = np.max(np.abs(ase_stress - lammps_stress))
                assert np.allclose(
                    ase_stress,
                    lammps_stress,
                    atol=tol,
                    rtol=tol,
                ), f"Stress max abs err: {err:.8g} (tol={tol:.3g})"
